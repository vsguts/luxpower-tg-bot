import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
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
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._sink = sink
        self._runner = runner
        self._scenes = scenes
        self._retry_delays = retry_delays
        self._clock = clock
        self._task: asyncio.Task | None = None

    async def publish(self, events: Sequence[Event]) -> None:
        # A journal that cannot be written must not keep the house on the grid's settings.
        try:
            await self._sink.publish(events)
        finally:
            for event in events:
                if event.type in GRID_EVENTS:
                    self._start(event.type)

    # A scene still retrying for the previous grid change would undo this one if it got through late.
    def _start(self, cause: EventType) -> None:
        if self._task is not None:
            self._task.cancel()
        scene = self._scenes.get(cause)
        # The poller waits for publish(); a slow cloud must not delay its next reading.
        self._task = asyncio.create_task(self._run(cause, scene), name=f"tuya-{cause}") if scene else None

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        await asyncio.wait({task}, timeout=CLOSE_GRACE)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self, cause: EventType, scene: str) -> None:
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
            return
        await self._report(Event(EventType.TUYA_SCENE_FAILED, self._clock(), cause=cause, detail=str(error)))

    async def _report(self, event: Event) -> None:
        try:
            await self._sink.publish([event])
        except Exception:
            log.exception("reporting %s failed", event.type)
