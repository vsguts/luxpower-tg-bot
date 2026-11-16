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
# DSM leaves Safe Mode only when the UPS is on line again. Long enough for it to see that and come back, short
# enough that a forgotten wake does not hide an outage for long; DSM set to "until low battery" stays up after.
WAKE_FOR = timedelta(minutes=10)


class Readings(Protocol):
    latest: Snapshot | None
    state: State | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InverterUps:
    """The inverter as NUT clients see it, with the admin's overrides on top: the low-battery test (kept until
    /ups_reset or a restart) and the wake (grid reported on for WAKE_FOR)."""

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
        self.online_until: datetime | None = None

    @property
    def waking(self) -> bool:
        return self.online_until is not None and self._clock() < self.online_until

    def wake(self) -> datetime | None:
        """Reports the grid as on for WAKE_FOR, so a NAS sent to Safe Mode during an outage comes back. Refused (None)
        on a low battery: the NAS would have to go down again at once."""
        self.test_low = False
        self.online_until = None
        variables = self.variables()
        if variables is None or "LB" in variables["ups.status"]:
            return None
        self.online_until = self._clock() + WAKE_FOR
        return self.online_until

    def variables(self) -> dict[str, str] | None:
        readings, now = self._readings, self._clock()
        self._track(readings.latest, readings.state)
        variables = ups_variables(
            readings.latest,
            readings.state,
            now,
            self._config.low_soc,
            self._battery,
            low_confirmed=self._low,
            reading_overdue=now - self._started > READING_LOST_AFTER,
            test_low=self.test_low,
        )
        # Only a plain "on battery" is masked: a low battery still reaches the NAS.
        if variables is not None and variables["ups.status"] == "OB" and self.waking:
            variables["ups.status"] = "OL"
        return variables

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
