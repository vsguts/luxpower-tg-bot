import asyncio
import logging
from collections.abc import Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import BotCommand

from lxbot.bot.handlers import router
from lxbot.config import Settings, settings_from_env
from lxbot.luxpower.client import DongleClient
from lxbot.monitor.poller import Poller
from lxbot.notify.broadcast import Notifier
from lxbot.storage.db import Database

log = logging.getLogger(__name__)

TELEGRAM_RETRY_DELAY = 30.0
_UNREACHABLE = (TelegramNetworkError, TelegramServerError)

COMMANDS = [
    BotCommand(command="start", description="Підписатися на сповіщення"),
    BotCommand(command="status", description="Стан мережі, генератора і батареї"),
    BotCommand(command="stop", description="Відписатися"),
]


# After a blackout the house is back before the ISP: keep the monitor running and wait for Telegram.
async def wait_for_telegram(bot: Bot, retry_delay: float = TELEGRAM_RETRY_DELAY) -> None:
    while True:
        try:
            await bot.set_my_commands(COMMANDS)
            return
        except _UNREACHABLE as exc:
            log.warning("Telegram is unreachable, retrying in %.0f s: %s", retry_delay, exc)
            await asyncio.sleep(retry_delay)


async def poll_telegram(dispatcher: Dispatcher, bot: Bot, retry_delay: float = TELEGRAM_RETRY_DELAY) -> None:
    while True:
        try:
            await dispatcher.start_polling(bot)
            return
        except _UNREACHABLE as exc:
            log.warning("Telegram polling failed, restarting in %.0f s: %s", retry_delay, exc)
            await asyncio.sleep(retry_delay)


# Strong references: the event loop keeps only weak ones to running tasks.
_shutdowns: set[asyncio.Task] = set()


async def _shut_down(dispatcher: Dispatcher, main: asyncio.Task) -> None:
    try:
        await dispatcher.stop_polling()
    except RuntimeError:  # polling has not started yet: still waiting for Telegram
        main.cancel()


def stop_when_ended(dispatcher: Dispatcher, main: asyncio.Task) -> Callable[[asyncio.Task], None]:
    # A dead poller or notifier must not leave a bot that answers but never alerts: exit and let Docker restart.
    def on_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        log.error("%s stopped unexpectedly, shutting down", task.get_name(), exc_info=task.exception())
        shutdown = asyncio.create_task(_shut_down(dispatcher, main))
        _shutdowns.add(shutdown)
        shutdown.add_done_callback(_shutdowns.discard)

    return on_done


async def run(settings: Settings) -> None:
    db = await Database.open(settings.db_path)
    client = DongleClient(settings.dongle)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db, settings.admin_chat_id, settings.tz)
    poller = Poller(client, db, notifier, settings.thresholds, settings.poll_interval)
    dispatcher = Dispatcher(db=db, settings=settings, poller=poller)
    dispatcher.include_router(router)
    background: list[asyncio.Task] = []
    on_done = stop_when_ended(dispatcher, asyncio.current_task())
    try:
        await poller.load()
        background = [
            asyncio.create_task(notifier.run(), name="notifier"),
            asyncio.create_task(poller.run(), name="poller"),
        ]
        for task in background:
            task.add_done_callback(on_done)
        await wait_for_telegram(bot)
        await poll_telegram(dispatcher, bot)
    finally:
        for task in background:
            task.remove_done_callback(on_done)
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await client.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(settings_from_env()))


if __name__ == "__main__":
    main()
