"""EcoFlow Delta Pro quota fields, as the cloud reports them, mapped onto the shared Snapshot.

Units come from a real Delta Pro dump (tools/dump_ecoflow.py): AC input voltage in mV, frequency in whole Hz,
powers in W. Solar power is unconfirmed until a dump taken while the panels deliver.
"""

from collections.abc import Mapping
from datetime import datetime

from lxbot.device.snapshot import Snapshot

SOC = "pd.soc"
AC_IN_MILLIVOLTS = "inv.acInVol"
AC_IN_HZ = "inv.acInFreq"
BATTERY_IN_W = "bmsMaster.inputWatts"
BATTERY_OUT_W = "bmsMaster.outputWatts"
SOLAR_W = "mppt.inWatts"
OUTPUT_W = "pd.wattsOutSum"


class InvalidQuota(ValueError):
    pass


def _number(quota: Mapping[str, object], key: str) -> int | float:
    value = quota.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidQuota(f"{key} is missing or not a number: {value!r}")
    return value


def decode_quota(quota: Mapping[str, object], taken_at: datetime) -> Snapshot:
    soc = int(_number(quota, SOC))
    if not 0 <= soc <= 100:
        raise InvalidQuota(f"implausible SOC {soc}%")
    return Snapshot(
        taken_at=taken_at,
        soc=soc,
        battery_w=int(_number(quota, BATTERY_IN_W) - _number(quota, BATTERY_OUT_W)),
        pv_w=int(_number(quota, SOLAR_W)),
        load_w=int(_number(quota, OUTPUT_W)),
        grid_v=_number(quota, AC_IN_MILLIVOLTS) / 1000,
        grid_hz=_number(quota, AC_IN_HZ),
        # A generator on the AC input is indistinguishable from the grid: it reads as the grid being back.
        gen_running=False,
    )
