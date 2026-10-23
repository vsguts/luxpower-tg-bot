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
# One failed repeat is a blip; the admin hears of it when the next one fails too.
REPEAT_FAILURES_TO_REPORT = 2


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
        grid_out: Callable[[], bool] = lambda: True,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._sink = sink
        self._runner = runner
        self._scenes = scenes
        self._retry_delays = retry_delays
        self._repeat = repeat
        self._grid_out = grid_out
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

    # The bot (re)started while the stored state says the grid is out: no GRID_LOST comes for this outage. The
    # scene ran before the restart, and the grid may be back by now (its return still being debounced), so only
    # the repeats resume, each checking the live grid first.
    def resume_outage(self) -> None:
        self._cancel()
        scene = self._scenes.get(EventType.GRID_LOST)
        if scene is not None and self._repeat is not None:
            self._repeats = asyncio.create_task(self._repeat_loop(scene, reported_failure=False), name="tuya-repeat")

    def _cancel(self) -> None:
        for task in (self._task, self._repeats):
            if task is not None:
                task.cancel()
        self._task = self._repeats = None

    # A scene still retrying for the previous grid change would undo this one if it got through late.
    def _start(self, cause: EventType) -> None:
        self._cancel()
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
    # card would get a write every few minutes), and the admin hears of failing repeats once per outage.
    async def _repeat_loop(self, scene: str, reported_failure: bool) -> None:
        failures = 0
        while True:
            await asyncio.sleep(self._repeat.total_seconds())
            # The grid may be back while its return is debounced: switching devices off then would undo the
            # people who just switched them on.
            if not self._current_repeats():
                return
            if not self._grid_out():
                continue
            try:
                await self._runner.trigger_scene(scene)
            except Exception as exc:
                # The HTTP stack can turn a cancellation into a timeout: a superseded loop must not live on.
                if not self._current_repeats():
                    return
                failures += 1
                log.warning("tuya scene %s repeat failed: %s", scene, exc)
                if failures >= REPEAT_FAILURES_TO_REPORT and not reported_failure:
                    reported_failure = True
                    await self._report(self._failure(EventType.GRID_LOST, exc))
                continue
            failures = 0

    def _current_repeats(self) -> bool:
        return self._repeats is not None and self._repeats is asyncio.current_task()

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
