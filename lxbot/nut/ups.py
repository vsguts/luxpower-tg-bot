from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from lxbot.device.snapshot import Snapshot
from lxbot.monitor.detector import State
from lxbot.monitor.forecast import BatteryConfig
from lxbot.nut.server import UpsConfig
from lxbot.nut.status import ups_variables


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
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._readings = readings
        self._config = config
        self._battery = battery
        self._clock = clock
        self.test_low = False

    def variables(self) -> dict[str, str] | None:
        readings = self._readings
        return ups_variables(
            readings.latest, readings.state, self._clock(), self._config.low_soc, self._battery, test_low=self.test_low
        )
