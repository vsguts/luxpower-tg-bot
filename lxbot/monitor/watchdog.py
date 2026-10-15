from datetime import datetime, timedelta

from lxbot.monitor.detector import Event, EventType

DONGLE_DOWN_AFTER = timedelta(minutes=5)


class DongleWatch:
    def __init__(self, down_after: timedelta = DONGLE_DOWN_AFTER):
        self._down_after = down_after
        self._failing_since: datetime | None = None
        self._down_reported = False

    def success(self, now: datetime) -> list[Event]:
        self._failing_since = None
        if not self._down_reported:
            return []
        self._down_reported = False
        return [Event(EventType.DONGLE_UP, now)]

    def failure(self, now: datetime) -> list[Event]:
        if self._failing_since is None:
            self._failing_since = now
        if self._down_reported or now - self._failing_since < self._down_after:
            return []
        self._down_reported = True
        return [Event(EventType.DONGLE_DOWN, self._failing_since)]
