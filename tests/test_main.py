from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.methods import GetMe

from lxbot.main import COMMANDS, poll_telegram, wait_for_telegram

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
