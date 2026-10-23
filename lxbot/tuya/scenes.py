import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from lxbot.monitor.detector import Event, EventType
from lxbot.monitor.poller import EventSink

log = logging.getLogger(__name__)

# The cloud or the home internet may blink right when the grid goes: retry for about a minute.
RETRY_DELAYS = (10.0, 50.0)
# A restart right after a grid change still lets a scene that is on its way finish; with the notifier's
# drain it stays inside the container's stop timeout.
CLOSE_GRACE = 3.0
GRID_EVENTS = frozenset({EventType.GRID_LOST, EventType.GRID_RESTORED})


class SceneRunner(Protocol):
    async def trigger_scene(self, rule_id: str) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SceneTrigger:
    """Runs a smart-home scene on a grid change, once the grid alert is on its way."""

    def __init__(
        self,
        sink: EventSink,
        runner: SceneRunner,
        scenes: Mapping[EventType, str],
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        repeat: timedelta | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._sink = sink
        self._runner = runner
        self._scenes = scenes
        self._retry_delays = retry_delays
        self._repeat = repeat
        self._clock = clock
        self._task: asyncio.Task | None = None
        # The repeats of a settled grid-lost scene: nothing on its way, so close() need not wait for them.
        self._repeats: asyncio.Task | None = None

    async def publish(self, events: Sequence[Event]) -> None:
        # A journal that cannot be written must not keep the house on the grid's settings.
        try:
            await self._sink.publish(events)
        finally:
            for event in events:
                if event.type in GRID_EVENTS:
                    self._start(event.type)

    # The bot (re)started while the grid was already out: no GRID_LOST will come for this outage.
    def resume_outage(self) -> None:
        self._start(EventType.GRID_LOST)

    # A scene still retrying for the previous grid change would undo this one if it got through late.
    def _start(self, cause: EventType) -> None:
        for task in (self._task, self._repeats):
            if task is not None:
                task.cancel()
        self._repeats = None
        scene = self._scenes.get(cause)
        # The poller waits for publish(); a slow cloud must not delay its next reading.
        self._task = asyncio.create_task(self._run(cause, scene), name=f"tuya-{cause}") if scene else None

    async def close(self) -> None:
        task, repeats = self._task, self._repeats
        self._task = self._repeats = None
        if repeats is not None:
            repeats.cancel()
        if task is not None:
            await asyncio.wait({task}, timeout=CLOSE_GRACE)
            task.cancel()
        await asyncio.gather(*(t for t in (task, repeats) if t is not None), return_exceptions=True)

    async def _run(self, cause: EventType, scene: str) -> None:
        reported_failure = not await self._run_with_retries(cause, scene)
        if cause is EventType.GRID_LOST and self._repeat is not None and self._task is asyncio.current_task():
            self._repeats = asyncio.create_task(self._repeat_loop(scene, reported_failure), name="tuya-repeat")

    # A device switched on during the outage goes off again within one repeat. Repeats are not journaled (the
    # card would get a write every few minutes), and the admin hears of a failure once per outage.
    async def _repeat_loop(self, scene: str, reported_failure: bool) -> None:
        cause = EventType.GRID_LOST
        while True:
            await asyncio.sleep(self._repeat.total_seconds())
            try:
                await self._runner.trigger_scene(scene)
            except Exception as exc:
                log.warning("tuya scene %s repeat failed: %s", scene, exc)
                if not reported_failure:
                    reported_failure = True
                    await self._report(self._failure(cause, exc))

    async def _run_with_retries(self, cause: EventType, scene: str) -> bool:
        error: Exception | None = None
        for delay in (0.0, *self._retry_delays):
            await asyncio.sleep(delay)
            try:
                await self._runner.trigger_scene(scene)
            except Exception as exc:
                log.warning("tuya scene %s for %s failed: %s", scene, cause, exc)
                error = exc
                continue
            await self._report(Event(EventType.TUYA_SCENE_RUN, self._clock(), cause=cause, detail=scene))
            return True
        await self._report(self._failure(cause, error))
        return False

    def _failure(self, cause: EventType, error: Exception | None) -> Event:
        return Event(EventType.TUYA_SCENE_FAILED, self._clock(), cause=cause, detail=str(error))

    async def _report(self, event: Event) -> None:
        try:
            await self._sink.publish([event])
        except Exception:
            log.exception("reporting %s failed", event.type)
