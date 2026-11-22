import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramServerError
from aiogram.methods import SendMessage
from aiohttp import ClientConnectorError

from lxbot.monitor.detector import Event, EventType
from lxbot.notify.broadcast import Notifier
from lxbot.storage.db import SubscriberStatus
from tests.factories import at

ADMIN = 1
METHOD = SendMessage(chat_id=0, text="x")


def timed_out() -> TelegramNetworkError:
    # As aiogram raises it: Telegram may have taken the request and only the answer got lost.
    exc = TelegramNetworkError(METHOD, "Request timeout error")
    exc.__cause__ = TimeoutError()
    return exc


def not_connected() -> TelegramNetworkError:
    key = SimpleNamespace(host="api.telegram.org", port=443, ssl=True)
    exc = TelegramNetworkError(METHOD, "ClientConnectorError: Cannot connect to host")
    exc.__cause__ = ClientConnectorError(key, OSError(111, "Connection refused"))
    return exc


class FakeSender:
    def __init__(self, failures: dict[int, list[Exception]] | None = None, get_me_failures: int = 0):
        self.sent: list[tuple[int, str]] = []
        self.failures = failures or {}
        self.calls: list[str] = []
        self.get_me_failures = get_me_failures

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.calls.append(f"send {chat_id}")
        if self.failures.get(chat_id):
            raise self.failures[chat_id].pop(0)
        self.sent.append((chat_id, text))

    async def get_me(self):
        self.calls.append("get_me")
        if self.get_me_failures:
            self.get_me_failures -= 1
            raise TelegramServerError(METHOD, "Bad Gateway")


@pytest.fixture
async def subscribers(db):
    await db.save_subscriber(10, "A", None, SubscriberStatus.APPROVED)
    await db.save_subscriber(11, "B", None, SubscriberStatus.APPROVED)
    await db.save_subscriber(12, "C", None, SubscriberStatus.PENDING)


async def deliver(notifier: Notifier, events) -> None:
    worker = asyncio.create_task(notifier.run())
    await notifier.publish(events)
    await asyncio.wait_for(notifier.join(), 2)
    worker.cancel()


def make_notifier(sender, db) -> Notifier:
    return Notifier(sender, db, [ADMIN], ZoneInfo("Europe/Kyiv"), retry_base=0)


async def test_subscriber_events_go_to_approved_subscribers(db, subscribers):
    sender = FakeSender()
    await deliver(make_notifier(sender, db), [Event(EventType.BATTERY_LOW, at(0), 30)])
    assert [chat_id for chat_id, _ in sender.sent] == [10, 11]
    assert [e.type for e in await db.recent_events()] == ["battery_low"]


async def test_admin_events_go_to_admin_only(db, subscribers):
    sender = FakeSender()
    await deliver(make_notifier(sender, db), [Event(EventType.DEVICE_DOWN, at(0))])
    assert [chat_id for chat_id, _ in sender.sent] == [ADMIN]


async def test_journal_only_events_are_not_sent(db, subscribers):
    sender = FakeSender()
    await deliver(make_notifier(sender, db), [Event(EventType.GEN_STARTED, at(0), 50)])
    assert sender.sent == []
    assert [e.type for e in await db.recent_events()] == ["gen_started"]


async def test_a_message_that_never_left_is_retried_until_sent(db, subscribers):
    sender = FakeSender({10: [not_connected(), not_connected(), not_connected()]})
    await deliver(make_notifier(sender, db), [Event(EventType.BATTERY_LOW, at(0), 30)])
    assert [chat_id for chat_id, _ in sender.sent] == [10, 11]
    assert "get_me" not in sender.calls


# 2026-11-20: Telegram took every timed-out send and delivered all 19 copies once it recovered.
async def test_a_message_telegram_may_have_is_resent_once_after_telegram_answers(db, subscribers):
    sender = FakeSender({10: [timed_out(), timed_out(), timed_out()]}, get_me_failures=2)
    await deliver(make_notifier(sender, db), [Event(EventType.GRID_RESTORED, at(1), 81, at(1) - at(0))])
    assert sender.calls == ["send 10", "get_me", "get_me", "get_me", "send 10", "send 11"]
    assert [chat_id for chat_id, _ in sender.sent] == [11]


async def test_a_server_error_counts_as_maybe_delivered(db, subscribers):
    sender = FakeSender({10: [TelegramServerError(METHOD, "Bad Gateway")]})
    await deliver(make_notifier(sender, db), [Event(EventType.BATTERY_LOW, at(0), 30)])
    assert sender.calls == ["send 10", "get_me", "send 10", "send 11"]


async def test_blocked_user_becomes_inactive(db, subscribers):
    sender = FakeSender({10: [TelegramForbiddenError(METHOD, "Forbidden: bot was blocked by the user")]})
    await deliver(make_notifier(sender, db), [Event(EventType.BATTERY_LOW, at(0), 30)])
    assert (await db.get_subscriber(10)).status is SubscriberStatus.INACTIVE
    assert [chat_id for chat_id, _ in sender.sent] == [11]


async def test_publish_does_not_wait_for_delivery(db, subscribers):
    class HangingSender:
        async def send_message(self, chat_id: int, text: str, **kwargs):
            await asyncio.Event().wait()

    notifier = make_notifier(HangingSender(), db)
    worker = asyncio.create_task(notifier.run())
    await asyncio.wait_for(notifier.publish([Event(EventType.GRID_LOST, at(1), 80, at(1) - at(0))]), 1)
    worker.cancel()
    assert [e.type for e in await db.recent_events()] == ["grid_lost"]


async def test_silent_events_are_sent_without_a_sound(db, subscribers):
    calls = []

    class RecordingSender:
        async def send_message(self, chat_id: int, text: str, **kwargs):
            calls.append(kwargs.get("disable_notification"))

    events = [Event(EventType.BATTERY_LOW, at(0), 30), Event(EventType.BATTERY_LOW, at(1), 29, silent=True)]
    await deliver(make_notifier(RecordingSender(), db), events)
    assert calls == [False, False, True, True]


async def test_without_a_generator_alerts_do_not_mention_one(db, subscribers):
    sender = FakeSender()
    notifier = Notifier(sender, db, [ADMIN], ZoneInfo("Europe/Kyiv"), generator=False, retry_base=0)
    await deliver(notifier, [Event(EventType.BATTERY_CRITICAL, at(0), 15)])
    assert sender.sent and all("генератор" not in text.lower() for _, text in sender.sent)


async def test_admin_events_go_to_every_admin(db, subscribers):
    sender = FakeSender()
    notifier = Notifier(sender, db, [ADMIN, 2], ZoneInfo("Europe/Kyiv"), retry_base=0)
    await deliver(notifier, [Event(EventType.DEVICE_DOWN, at(0))])
    assert [chat_id for chat_id, _ in sender.sent] == [ADMIN, 2]
