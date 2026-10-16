"""The one reading of a power source the monitor works with, whichever device it comes from."""

from dataclasses import dataclass
from datetime import datetime

MIN_AC_VOLTAGE = 180.0
# Hysteresis: a sagging feeder hovering around 180 V would otherwise flap between lost and restored.
GRID_HOLD_VOLTAGE = 170.0
MIN_AC_FREQUENCY = 45.0


class DeviceError(Exception):
    pass


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    soc: int
    # Positive while the battery charges, negative while it discharges.
    battery_w: int
    pv_w: int
    load_w: int
    grid_v: float
    grid_hz: float
    gen_running: bool

    @property
    def grid_present(self) -> bool:
        return self.grid_present_given(currently_present=False)

    def grid_present_given(self, currently_present: bool) -> bool:
        min_voltage = GRID_HOLD_VOLTAGE if currently_present else MIN_AC_VOLTAGE
        return self.grid_v > min_voltage and self.grid_hz > MIN_AC_FREQUENCY
