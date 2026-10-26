from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from lxbot.device.snapshot import Snapshot
from lxbot.monitor.detector import State
from lxbot.monitor.forecast import BatteryConfig
from lxbot.nut.server import UpsConfig
from lxbot.nut.status import READING_LOST_AFTER, ups_variables

# Once reported, a low battery holds until the charge is this far above the threshold: a 6 % / 7 % jitter
# would otherwise clear it before the NAS acts on it.
LOW_RELEASE_MARGIN = 2


class Readings(Protocol):
    latest: Snapshot | None
    state: State | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InverterUps:
    """The inverter as NUT clients see it, with the admin's low-battery test on top (kept until /ups_reset or a
    restart)."""

    def __init__(
        self,
        readings: Readings,
        config: UpsConfig,
        battery: BatteryConfig | None,
        *,
        confirm_polls: int = 2,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._readings = readings
        self._config = config
        self._battery = battery
        self._confirm_polls = confirm_polls
        self._clock = clock
        self._started = clock()
        self._seen: datetime | None = None
        self._low_streak = 0
        self._low = False
        self.test_low = False

    def variables(self) -> dict[str, str] | None:
        readings, now = self._readings, self._clock()
        self._track(readings.latest, readings.state)
        return ups_variables(
            readings.latest,
            readings.state,
            now,
            self._config.low_soc,
            self._battery,
            low_confirmed=self._low,
            reading_overdue=now - self._started > READING_LOST_AFTER,
            test_low=self.test_low,
        )

    # One garbled reading (a 0 between two 60s has been seen) must not take the NAS down: the charge has to
    # stay low for several readings, as for the battery alerts.
    def _track(self, snapshot: Snapshot | None, state: State | None) -> None:
        if snapshot is None or state is None or snapshot.taken_at == self._seen:
            return
        self._seen = snapshot.taken_at
        if state.grid.value or state.gen.value:
            self._low_streak, self._low = 0, False
            return
        low_soc = self._config.low_soc
        if snapshot.soc <= low_soc:
            self._low_streak += 1
            self._low = self._low or self._low_streak >= self._confirm_polls
            return
        self._low_streak = 0
        if snapshot.soc >= low_soc + LOW_RELEASE_MARGIN:
            self._low = False
