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
from aiohttp import ClientConnectorError

from lxbot.bot.texts import event_text
from lxbot.monitor.detector import Audience, Event
from lxbot.storage.db import Database, SubscriberStatus

log = logging.getLogger(__name__)

_RETRY_MAX = 60.0
# A send that timed out or met a server error may have reached Telegram all the same: on 2026-11-20 it took every
# one of 19 retries during an outage and delivered them all at once. One resend, once Telegram answers again, still
# covers a request it really lost.
MAX_UNCERTAIN_RESENDS = 1


class Sender(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs) -> object: ...

    async def get_me(self) -> object: ...


def _never_left(exc: TelegramNetworkError | TelegramServerError) -> bool:
    # aiogram chains aiohttp's error: a connection that never opened carried no request.
    return isinstance(exc.__cause__, ClientConnectorError)


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
        # The text carries the event time, so a late alert is still useful: keep trying what surely did not arrive.
        delay = self._retry_base
        resends = 0
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
                if _never_left(exc):
                    log.warning("sending to %s failed, retrying in %.0f s: %s", chat_id, delay, exc)
                    await asyncio.sleep(delay)
                    delay = self._next_delay(delay)
                    continue
                if resends >= MAX_UNCERTAIN_RESENDS:
                    log.error("not resending to %s again: Telegram may have the message already (%s)", chat_id, exc)
                    return
                resends += 1
                log.warning("sending to %s may have failed (%s); resending once Telegram answers", chat_id, exc)
                await self._wait_for_telegram()

    async def _wait_for_telegram(self) -> None:
        delay = self._retry_base
        while True:
            try:
                await self._sender.get_me()
                return
            except (TelegramNetworkError, TelegramServerError):
                await asyncio.sleep(delay)
                delay = self._next_delay(delay)

    def _next_delay(self, delay: float) -> float:
        return min(max(delay * 2, self._retry_base), _RETRY_MAX)
