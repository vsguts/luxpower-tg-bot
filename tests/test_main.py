import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.methods import GetMe

from lxbot.main import COMMANDS, poll_telegram, stop_background, stop_when_ended, wait_for_telegram
from lxbot.monitor.detector import Event, EventType
from lxbot.notify.broadcast import Notifier
from lxbot.storage.db import SubscriberStatus
from tests.factories import at

METHOD = GetMe()


class FlakyCall:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if self.failures:
            raise self.failures.pop(0)


def network_error():
    return TelegramNetworkError(METHOD, "timeout")


async def test_wait_for_telegram_retries_network_errors():
    set_my_commands = FlakyCall([network_error(), network_error()])
    bot = SimpleNamespace(set_my_commands=set_my_commands)
    await wait_for_telegram(bot, retry_delay=0)
    assert set_my_commands.calls == [(COMMANDS,)] * 3


async def test_wait_for_telegram_retries_server_errors():
    set_my_commands = FlakyCall([TelegramServerError(METHOD, "Bad Gateway")])
    bot = SimpleNamespace(set_my_commands=set_my_commands)
    await wait_for_telegram(bot, retry_delay=0)
    assert len(set_my_commands.calls) == 2


async def test_poll_telegram_restarts_polling_after_a_network_error():
    start_polling = FlakyCall([network_error()])
    dispatcher = SimpleNamespace(start_polling=start_polling)
    bot = object()
    await poll_telegram(dispatcher, bot, retry_delay=0)
    assert start_polling.calls == [(bot,), (bot,)]


async def ended(outcome):
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


async def test_a_background_task_that_dies_stops_polling():
    dispatcher = SimpleNamespace(stop_polling=AsyncMock())
    task = asyncio.create_task(ended(RuntimeError("bug")))
    task.add_done_callback(stop_when_ended(dispatcher, asyncio.current_task()))
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    dispatcher.stop_polling.assert_awaited_once()


async def test_a_background_task_that_returns_stops_polling():
    dispatcher = SimpleNamespace(stop_polling=AsyncMock())
    task = asyncio.create_task(ended(None))
    task.add_done_callback(stop_when_ended(dispatcher, asyncio.current_task()))
    await task
    await asyncio.sleep(0)
    dispatcher.stop_polling.assert_awaited_once()


async def test_a_cancelled_background_task_is_a_normal_shutdown():
    dispatcher = SimpleNamespace(stop_polling=AsyncMock())
    task = asyncio.create_task(asyncio.sleep(10))
    task.add_done_callback(stop_when_ended(dispatcher, asyncio.current_task()))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    dispatcher.stop_polling.assert_not_awaited()


async def test_a_task_that_dies_before_polling_cancels_the_startup():
    dispatcher = SimpleNamespace(stop_polling=AsyncMock(side_effect=RuntimeError("Polling is not started")))
    main = asyncio.create_task(asyncio.sleep(10))
    task = asyncio.create_task(ended(RuntimeError("bug")))
    task.add_done_callback(stop_when_ended(dispatcher, main))
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.gather(main, return_exceptions=True)
    assert main.cancelled()


class SlowSender:
    def __init__(self, delay):
        self.delay = delay
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        await asyncio.sleep(self.delay)
        self.sent.append(chat_id)


async def start_background(db, sender):
    await db.save_subscriber(10, "A", None, SubscriberStatus.APPROVED)
    notifier = Notifier(sender, db, 1, ZoneInfo("Europe/Kyiv"))
    notifier_task = asyncio.create_task(notifier.run())
    poller_task = asyncio.create_task(asyncio.sleep(10))
    await notifier.publish([Event(EventType.GRID_LOST, at(0), 80, previous_duration=at(0) - at(0))])
    return poller_task, notifier_task, notifier


async def test_shutdown_delivers_queued_messages_first(db):
    sender = SlowSender(0.01)
    poller_task, notifier_task, notifier = await start_background(db, sender)
    await stop_background(poller_task, notifier_task, notifier)
    assert sender.sent == [10]
    assert poller_task.cancelled() and notifier_task.cancelled()


async def test_shutdown_gives_up_on_a_stuck_queue(db):
    sender = SlowSender(10)
    poller_task, notifier_task, notifier = await start_background(db, sender)
    await asyncio.wait_for(stop_background(poller_task, notifier_task, notifier, drain_seconds=0.01), 1)
    assert sender.sent == []
    assert notifier_task.cancelled()


async def test_stop_background_cancels_extra_tasks(db):
    notifier = Notifier(SimpleNamespace(send_message=AsyncMock()), db, 1, ZoneInfo("Europe/Kyiv"))
    poller_task = asyncio.create_task(asyncio.sleep(3600))
    notifier_task = asyncio.create_task(notifier.run())
    schedule_task = asyncio.create_task(asyncio.sleep(3600))
    await stop_background(poller_task, notifier_task, notifier, 0.1, others=[schedule_task])
    assert schedule_task.cancelled()
