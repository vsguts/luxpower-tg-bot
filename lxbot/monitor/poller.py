import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from lxbot.device.snapshot import DeviceError, Snapshot
from lxbot.monitor.detector import Event, State, Thresholds, detect
from lxbot.monitor.watchdog import DeviceWatch
from lxbot.storage.db import Database

log = logging.getLogger(__name__)

STATE_KEY = "detector"


class SnapshotSource(Protocol):
    async def read_snapshot(self, taken_at: datetime) -> Snapshot: ...


class EventSink(Protocol):
    async def publish(self, events: Sequence[Event]) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Poller:
    def __init__(
        self,
        source: SnapshotSource,
        db: Database,
        sink: EventSink,
        thresholds: Thresholds,
        interval: float,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._source = source
        self._db = db
        self._sink = sink
        self._thresholds = thresholds
        self._interval = interval
        self._clock = clock
        self._watch = DeviceWatch()
        self._saved_state: dict | None = None
        self.latest: Snapshot | None = None
        self.state: State | None = None

    async def load(self) -> None:
        self._saved_state = await self._db.load_state(STATE_KEY)
        self.state = State.from_dict(self._saved_state) if self._saved_state else None

    async def poll_once(self) -> None:
        now = self._clock()
        try:
            snapshot = await self._source.read_snapshot(now)
        except DeviceError as exc:
            log.warning("poll failed: %s", exc)
            await self._sink.publish(self._watch.failure(now))
            return
        events = self._watch.success(now)
        self.latest = snapshot
        self.state, detected = detect(self.state, snapshot, self._thresholds)
        await self._persist_state()
        await self._sink.publish(events + detected)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await self.poll_once()
            except Exception:
                log.exception("unexpected error while polling")
            await asyncio.sleep(max(0.0, self._interval - (loop.time() - started)))

    async def _persist_state(self) -> None:
        # The database sits on the board's microSD card: write only when something changed.
        data = self.state.to_dict()
        if data != self._saved_state:
            await self._db.save_state(STATE_KEY, data)
            self._saved_state = data
