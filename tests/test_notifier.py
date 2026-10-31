import asyncio
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage

from lxbot.monitor.detector import Event, EventType
from lxbot.notify.broadcast import Notifier
from lxbot.storage.db import SubscriberStatus
from tests.factories import at

ADMIN = 1
METHOD = SendMessage(chat_id=0, text="x")


class FakeSender:
    def __init__(self, failures: dict[int, list[Exception]] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.failures = failures or {}

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if self.failures.get(chat_id):
            raise self.failures[chat_id].pop(0)
        self.sent.append((chat_id, text))


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
    return Notifier(sender, db, ADMIN, ZoneInfo("Europe/Kyiv"), retry_base=0)


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


async def test_network_errors_are_retried(db, subscribers):
    errors = [TelegramNetworkError(METHOD, "timeout"), TelegramNetworkError(METHOD, "timeout")]
    sender = FakeSender({10: errors})
    await deliver(make_notifier(sender, db), [Event(EventType.BATTERY_LOW, at(0), 30)])
    assert [chat_id for chat_id, _ in sender.sent] == [10, 11]


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
    notifier = Notifier(sender, db, ADMIN, ZoneInfo("Europe/Kyiv"), generator=False, retry_base=0)
    await deliver(notifier, [Event(EventType.BATTERY_CRITICAL, at(0), 15)])
    assert sender.sent and all("генератор" not in text.lower() for _, text in sender.sent)
