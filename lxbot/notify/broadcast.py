import asyncio
import logging
from collections.abc import Sequence
from datetime import tzinfo
from typing import Protocol

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from lxbot.bot.texts import event_text
from lxbot.monitor.detector import Audience, Event
from lxbot.storage.db import Database, SubscriberStatus

log = logging.getLogger(__name__)

_RETRY_MAX = 60.0


class Sender(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs) -> object: ...


class Notifier:
    def __init__(
        self,
        sender: Sender,
        db: Database,
        admin_chat_ids: Sequence[int],
        tz: tzinfo,
        *,
        generator: bool = True,
        retry_base: float = 1.0,
    ):
        self._sender = sender
        self._db = db
        self._admin_chat_ids = list(admin_chat_ids)
        self._tz = tz
        self._generator = generator
        self._retry_base = retry_base
        self._queue: asyncio.Queue[tuple[int, str, bool]] = asyncio.Queue()

    async def publish(self, events: Sequence[Event]) -> None:
        for event in events:
            await self._db.add_event(event.type, event.at, event.to_payload())
            text = event_text(event, self._tz, generator=self._generator)
            if text is None:
                continue
            for chat_id in await self._recipients(event.audience):
                self._queue.put_nowait((chat_id, text, event.silent))

    async def run(self) -> None:
        while True:
            chat_id, text, silent = await self._queue.get()
            try:
                await self._deliver(chat_id, text, silent)
            except Exception:
                log.exception("dropping a message to %s", chat_id)
            finally:
                self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    async def _recipients(self, audience: Audience) -> list[int]:
        if audience is Audience.ADMIN:
            return list(self._admin_chat_ids)
        return [s.chat_id for s in await self._db.subscribers(SubscriberStatus.APPROVED)]

    async def _deliver(self, chat_id: int, text: str, silent: bool = False) -> None:
        # Keep retrying network failures: the text carries the event time, so late is still useful.
        delay = self._retry_base
        while True:
            try:
                await self._sender.send_message(chat_id=chat_id, text=text, disable_notification=silent)
                return
            except TelegramForbiddenError:
                log.info("chat %s blocked the bot; marking inactive", chat_id)
                await self._db.set_subscriber_status(chat_id, SubscriberStatus.INACTIVE)
                return
            except TelegramBadRequest as exc:
                log.error("Telegram rejected a message to %s: %s", chat_id, exc)
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except (TelegramNetworkError, TelegramServerError) as exc:
                log.warning("sending to %s failed, retrying in %.0f s: %s", chat_id, delay, exc)
                await asyncio.sleep(delay)
                delay = min(max(delay * 2, self._retry_base), _RETRY_MAX)
