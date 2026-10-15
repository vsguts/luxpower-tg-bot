from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

from lxbot.monitor.detector import Event, EventType
from lxbot.monitor.poller import EventSink
from lxbot.schedule.compare import match_lost, match_restored
from lxbot.schedule.model import Schedule

_MATCHERS = {EventType.GRID_LOST: match_lost, EventType.GRID_RESTORED: match_restored}


class ScheduleProvider(Protocol):
    def current(self, now: datetime) -> Schedule | None: ...


class ScheduleAnnotator:
    def __init__(self, sink: EventSink, schedules: ScheduleProvider, window: timedelta):
        self._sink = sink
        self._schedules = schedules
        self._window = window

    async def publish(self, events: Sequence[Event]) -> None:
        await self._sink.publish([self._annotate(event) for event in events])

    def _annotate(self, event: Event) -> Event:
        matcher = _MATCHERS.get(event.type)
        if matcher is None:
            return event
        schedule = self._schedules.current(event.at)
        if schedule is None:
            return event
        return replace(event, match=matcher(schedule.timeline(), event.at, self._window))
