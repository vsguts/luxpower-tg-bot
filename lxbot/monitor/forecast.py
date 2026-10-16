"""How long the battery lasts while the house runs on it. Pure."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from lxbot.device.snapshot import Snapshot

# A trend over a few minutes or a single percent is noise, not a rate.
MIN_TREND_TIME = timedelta(minutes=30)
MIN_TREND_DROP = 2
# Both estimates are rough; minute precision would promise more than the BMS knows.
FORECAST_STEP = timedelta(minutes=10)
# A trickle of discharge (the sun almost covers the load) gives runtimes of days that only confuse.
LONG_RUNTIME = timedelta(days=2)


@dataclass(frozen=True)
class BatteryConfig:
    capacity_wh: float
    reserve_soc: int = 5


@dataclass(frozen=True)
class Forecast:
    power_w: int
    by_power: timedelta | None
    by_trend: timedelta | None = None
    trend_drop: int | None = None
    trend_elapsed: timedelta | None = None

    def to_payload(self) -> dict:
        return {
            "power_w": self.power_w,
            "by_power_s": _seconds(self.by_power),
            "by_trend_s": _seconds(self.by_trend),
            "trend_drop": self.trend_drop,
            "trend_elapsed_s": _seconds(self.trend_elapsed),
        }


def _seconds(duration: timedelta | None) -> int | None:
    return int(duration.total_seconds()) if duration is not None else None


# round() would send halves to the even step; a forecast reads better rounded half up.
def _rounded(duration: timedelta) -> timedelta:
    return FORECAST_STEP * math.floor(duration / FORECAST_STEP + 0.5)


def _usable_percent(soc: int, battery: BatteryConfig) -> int:
    return max(soc - battery.reserve_soc, 0)


def by_power(soc: int, power_w: int, battery: BatteryConfig) -> timedelta | None:
    if power_w <= 0:
        return None
    energy_wh = battery.capacity_wh * _usable_percent(soc, battery) / 100
    return _rounded(timedelta(hours=energy_wh / power_w))


def by_trend(
    start_soc: int, start_at: datetime, soc: int, now: datetime, battery: BatteryConfig
) -> timedelta | None:
    elapsed = now - start_at
    drop = start_soc - soc
    if elapsed < MIN_TREND_TIME or drop < MIN_TREND_DROP:
        return None
    # Multiply before dividing: timedelta / int rounds to microseconds and can nudge a half step down.
    return _rounded(elapsed * _usable_percent(soc, battery) / drop)


def forecast_for(
    snapshot: Snapshot,
    *,
    on_battery: bool,
    drain_soc: int | None,
    drain_since: datetime | None,
    battery: BatteryConfig | None,
) -> Forecast | None:
    power_w = -snapshot.battery_w
    if battery is None or not on_battery or power_w <= 0:
        return None
    trend = None
    if drain_soc is not None and drain_since is not None:
        trend = by_trend(drain_soc, drain_since, snapshot.soc, snapshot.taken_at, battery)
    return Forecast(
        power_w=power_w,
        by_power=by_power(snapshot.soc, power_w, battery),
        by_trend=trend,
        trend_drop=drain_soc - snapshot.soc if trend is not None else None,
        trend_elapsed=snapshot.taken_at - drain_since if trend is not None else None,
    )
