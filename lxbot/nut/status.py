"""The inverter seen as a UPS: Network UPS Tools variables from the monitor's state. Pure."""

from datetime import datetime, timedelta

from lxbot.device.snapshot import Snapshot
from lxbot.monitor.detector import State
from lxbot.monitor.forecast import BatteryConfig, forecast_for

MFR = "LuxPower"
MODEL = "SNA6000 WPV"
# The dongle runs off the inverter: silent this long, it is gone, not just between two polls.
READING_LOST_AFTER = timedelta(minutes=2)
# An inverter that shuts down at its cut-off takes the dongle with it, so the last reading is a few percent
# above the cut-off. A dongle lost at a higher charge is more likely a Wi-Fi problem.
SHUTDOWN_SOC = 15


def ups_variables(
    snapshot: Snapshot | None,
    state: State | None,
    now: datetime,
    low_soc: int,
    battery: BatteryConfig | None,
    *,
    test_low: bool = False,
) -> dict[str, str] | None:
    if state is None:
        return None
    on_battery = not state.grid.value and not state.gen.value
    fresh = snapshot is not None and now - snapshot.taken_at <= READING_LOST_AFTER
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
    variables["ups.status"] = _status(snapshot, on_battery, fresh, low_soc, test_low)
    return variables


def _status(snapshot: Snapshot | None, on_battery: bool, fresh: bool, low_soc: int, test_low: bool) -> str:
    if test_low:
        return "OB LB"
    if not on_battery:
        return "OL CHRG" if fresh and snapshot.battery_w > 0 else "OL"
    if snapshot is None:
        return "OB"
    low = snapshot.soc <= low_soc or (not fresh and snapshot.soc <= SHUTDOWN_SOC)
    return "OB LB" if low else "OB"
