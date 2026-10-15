import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from lxbot.bot.handlers import router
from lxbot.config import Settings, settings_from_env
from lxbot.luxpower.client import DongleClient
from lxbot.monitor.poller import Poller
from lxbot.notify.broadcast import Notifier
from lxbot.storage.db import Database

COMMANDS = [
    BotCommand(command="start", description="Підписатися на сповіщення"),
    BotCommand(command="status", description="Стан мережі, генератора і батареї"),
    BotCommand(command="stop", description="Відписатися"),
]


async def run(settings: Settings) -> None:
    db = await Database.open(settings.db_path)
    client = DongleClient(settings.dongle)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db, settings.admin_chat_id, settings.tz)
    poller = Poller(client, db, notifier, settings.thresholds, settings.poll_interval)
    dispatcher = Dispatcher(db=db, settings=settings, poller=poller)
    dispatcher.include_router(router)
    background = [asyncio.create_task(notifier.run()), asyncio.create_task(poller.run())]
    try:
        await bot.set_my_commands(COMMANDS)
        await dispatcher.start_polling(bot)
    finally:
        for task in background:
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
