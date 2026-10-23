import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import BotCommand

from lxbot.bot.handlers import router
from lxbot.config import Settings, settings_from_env
from lxbot.luxpower.client import DongleClient
from lxbot.monitor.detector import EventType
from lxbot.monitor.poller import EventSink, Poller
from lxbot.notify.broadcast import Notifier
from lxbot.schedule.annotate import ScheduleAnnotator
from lxbot.schedule.service import ScheduleService
from lxbot.schedule.source import HttpScheduleSource
from lxbot.storage.db import Database
from lxbot.tuya.api import TuyaApi
from lxbot.tuya.scenes import SceneTrigger

log = logging.getLogger(__name__)

TELEGRAM_RETRY_DELAY = 30.0
SHUTDOWN_DRAIN_SECONDS = 5.0
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


async def stop_background(
    poller_task: asyncio.Task,
    notifier_task: asyncio.Task,
    notifier: Notifier,
    drain_seconds: float = SHUTDOWN_DRAIN_SECONDS,
    *,
    others: Sequence[asyncio.Task] = (),
    flush: Callable[[], Awaitable[None]] | None = None,
) -> None:
    poller_task.cancel()
    for task in others:
        task.cancel()
    await asyncio.gather(poller_task, *others, return_exceptions=True)
    # Work the poller started (a smart-home scene) reports into the notifier, so it ends before the drain.
    if flush is not None:
        await flush()
    # An alert queued just before a restart still goes out, unless Telegram is unreachable.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(notifier.join(), drain_seconds)
    notifier_task.cancel()
    await asyncio.gather(notifier_task, return_exceptions=True)


def _scenes(settings: Settings) -> dict[EventType, str]:
    by_event = {
        EventType.GRID_LOST: settings.tuya.grid_lost_scene,
        EventType.GRID_RESTORED: settings.tuya.grid_restored_scene,
    }
    return {event: scene for event, scene in by_event.items() if scene}


async def run(settings: Settings) -> None:
    db = await Database.open(settings.db_path)
    client = DongleClient(settings.dongle)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db, settings.admin_chat_id, settings.tz)
    source = HttpScheduleSource(settings.schedule.url) if settings.schedule else None
    schedules = ScheduleService(source, db, notifier, settings.schedule, settings.tz) if source else None
    tuya = TuyaApi(settings.tuya) if settings.tuya else None
    scenes = SceneTrigger(notifier, tuya, _scenes(settings)) if tuya else None
    sink: EventSink = scenes or notifier
    if schedules is not None:
        sink = ScheduleAnnotator(sink, schedules, settings.schedule.match_window)
    poller = Poller(client, db, sink, settings.thresholds, settings.poll_interval)
    dispatcher = Dispatcher(db=db, settings=settings, poller=poller, schedules=schedules)
    dispatcher.include_router(router)
    background: list[asyncio.Task] = []
    on_done = stop_when_ended(dispatcher, asyncio.current_task())
    try:
        await poller.load()
        if schedules is not None:
            await schedules.load()
        background = [
            asyncio.create_task(poller.run(), name="poller"),
            asyncio.create_task(notifier.run(), name="notifier"),
        ]
        if schedules is not None:
            background.append(asyncio.create_task(schedules.run(), name="schedule"))
        for task in background:
            task.add_done_callback(on_done)
        await wait_for_telegram(bot)
        await poll_telegram(dispatcher, bot)
    finally:
        for task in background:
            task.remove_done_callback(on_done)
        if background:
            flush = scenes.close if scenes is not None else None
            await stop_background(background[0], background[1], notifier, others=background[2:], flush=flush)
        elif scenes is not None:
            await scenes.close()
        if source is not None:
            await source.close()
        if tuya is not None:
            await tuya.close()
        await client.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(settings_from_env()))


if __name__ == "__main__":
    main()
