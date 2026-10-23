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
# A restart right after a grid change still lets a scene that is on its way finish.
CLOSE_GRACE = 5.0


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
        self._tasks: set[asyncio.Task] = set()

    async def publish(self, events: Sequence[Event]) -> None:
        await self._sink.publish(events)
        for event in events:
            scene = self._scenes.get(event.type)
            if scene is None:
                continue
            # The poller waits for publish(); a slow cloud must not delay its next reading.
            task = asyncio.create_task(self._run(event.type, scene), name=f"tuya-{event.type}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=CLOSE_GRACE)
        pending = set(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

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
