"""The inverter seen as a UPS: Network UPS Tools variables from the monitor's state. Pure."""

from datetime import datetime, timedelta

from lxbot.device.snapshot import Snapshot
from lxbot.monitor.detector import State
from lxbot.monitor.forecast import BatteryConfig, forecast_for

MFR = "LuxPower"
MODEL = "SNA6000 WPV"
# The dongle runs off the inverter: silent this long, it is gone, not just between two polls.
READING_LOST_AFTER = timedelta(minutes=2)
# A dongle lost this close to the threshold leaves too little battery to wait for: the inverter is shutting
# down, or will within minutes.
LOST_NEAR_LOW = 2


def estimated_soc(snapshot: Snapshot, now: datetime, battery: BatteryConfig | None) -> float | None:
    """The charge left now if the battery kept draining at its last known power since the last reading."""
    if battery is None or snapshot.battery_w >= 0:
        return None
    hours = (now - snapshot.taken_at) / timedelta(hours=1)
    return snapshot.soc - (-snapshot.battery_w) * hours / battery.capacity_wh * 100


def ups_variables(
    snapshot: Snapshot | None,
    state: State | None,
    now: datetime,
    low_soc: int,
    battery: BatteryConfig | None,
    *,
    low_confirmed: bool = False,
    reading_overdue: bool = False,
    test_low: bool = False,
) -> dict[str, str] | None:
    """low_confirmed: the charge has stayed at or below low_soc for enough readings to trust it.
    reading_overdue: the bot has run long enough to have read the dongle, and has not."""
    if state is None:
        return None
    fresh = snapshot is not None and now - snapshot.taken_at <= READING_LOST_AFTER
    on_battery = not state.grid.value and not state.gen.value
    if not fresh and snapshot is not None:
        # The inverter may have died while a grid or generator loss was still being debounced: the last raw
        # reading tells more than a state that will never be updated again.
        on_battery = on_battery or (not snapshot.grid_present_given(state.grid.value) and not snapshot.gen_running)
    variables = {
        "device.mfr": MFR,
        "device.model": MODEL,
        "device.type": "ups",
        "ups.mfr": MFR,
        "ups.model": MODEL,
        "battery.charge.low": str(low_soc),
    }
    if snapshot is not None:
        variables["battery.charge"] = str(snapshot.soc)
    if fresh:
        variables["input.voltage"] = f"{snapshot.grid_v:.0f}"
        variables["ups.realpower"] = str(snapshot.load_w)
        forecast = forecast_for(snapshot, on_battery=on_battery, drain_soc=None, drain_since=None, battery=battery)
        if forecast is not None and forecast.by_power is not None:
            variables["battery.runtime"] = str(int(forecast.by_power.total_seconds()))
    if test_low:
        status = "OB LB"
    elif not on_battery:
        status = "OL CHRG" if fresh and snapshot.battery_w > 0 else "OL"
    else:
        low = low_confirmed or _low_without_reading(snapshot, state, now, low_soc, battery, fresh, reading_overdue)
        status = "OB LB" if low else "OB"
    variables["ups.status"] = status
    return variables


def _low_without_reading(
    snapshot: Snapshot | None,
    state: State,
    now: datetime,
    low_soc: int,
    battery: BatteryConfig | None,
    fresh: bool,
    reading_overdue: bool,
) -> bool:
    if fresh:
        return False
    if snapshot is None:
        # Restarted with the dongle gone: the stored critical alert (15 %) is the last trace of the charge.
        return reading_overdue and state.critical_sent
    if snapshot.soc <= low_soc + LOST_NEAR_LOW:
        return True
    estimate = estimated_soc(snapshot, now, battery)
    return estimate is not None and estimate <= low_soc
