import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.methods import GetMe

from lxbot.main import COMMANDS, poll_telegram, stop_when_ended, wait_for_telegram

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
    bot = type("FakeBot", (), {"set_my_commands": staticmethod(set_my_commands)})
    await wait_for_telegram(bot, retry_delay=0)
    assert set_my_commands.calls == [(COMMANDS,)] * 3


async def test_wait_for_telegram_retries_server_errors():
    set_my_commands = FlakyCall([TelegramServerError(METHOD, "Bad Gateway")])
    bot = type("FakeBot", (), {"set_my_commands": staticmethod(set_my_commands)})
    await wait_for_telegram(bot, retry_delay=0)
    assert len(set_my_commands.calls) == 2


async def test_poll_telegram_restarts_polling_after_a_network_error():
    start_polling = FlakyCall([network_error()])
    dispatcher = type("FakeDispatcher", (), {"start_polling": staticmethod(start_polling)})
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
