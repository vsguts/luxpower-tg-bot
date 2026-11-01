"""EcoFlow Delta Pro quota fields, as the cloud reports them, mapped onto the shared Snapshot.

Units come from real Delta Pro dumps (tools/dump_ecoflow.py): AC input voltage in mV, frequency in whole Hz,
battery current in mA and voltage in mV, powers in W. Solar power is unconfirmed until a dump taken while
the panels deliver.
"""

from collections.abc import Mapping
from datetime import datetime

from lxbot.device.snapshot import Snapshot

SOC = "pd.soc"
AC_IN_MILLIVOLTS = "inv.acInVol"
AC_IN_HZ = "inv.acInFreq"
BATTERY_MILLIAMPS = "bmsMaster.amp"
BATTERY_MILLIVOLTS = "bmsMaster.vol"
SOLAR_W = "mppt.inWatts"
OUTPUT_W = "pd.wattsOutSum"
# At rest on the grid the BMS reads about -135 mA (7 W): its own draw, not a flow worth reporting.
BMS_IDLE_W = 10


class InvalidQuota(ValueError):
    pass


def _number(quota: Mapping[str, object], key: str) -> int | float:
    value = quota.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidQuota(f"{key} is missing or not a number: {value!r}")
    return value


# The cloud leaves bmsMaster.inputWatts/outputWatts at 0 even while the battery runs the house.
def _battery_w(quota: Mapping[str, object]) -> int:
    watts = round(_number(quota, BATTERY_MILLIAMPS) * _number(quota, BATTERY_MILLIVOLTS) / 1_000_000)
    return 0 if abs(watts) < BMS_IDLE_W else watts


def decode_quota(quota: Mapping[str, object], taken_at: datetime) -> Snapshot:
    soc = int(_number(quota, SOC))
    if not 0 <= soc <= 100:
        raise InvalidQuota(f"implausible SOC {soc}%")
    return Snapshot(
        taken_at=taken_at,
        soc=soc,
        battery_w=_battery_w(quota),
        pv_w=int(_number(quota, SOLAR_W)),
        load_w=int(_number(quota, OUTPUT_W)),
        grid_v=_number(quota, AC_IN_MILLIVOLTS) / 1000,
        grid_hz=_number(quota, AC_IN_HZ),
        # A generator on the AC input is indistinguishable from the grid: it reads as the grid being back.
        gen_running=False,
    )
