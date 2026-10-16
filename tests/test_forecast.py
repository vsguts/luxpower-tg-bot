from datetime import timedelta

import pytest

from lxbot.monitor.forecast import BatteryConfig, Forecast, by_power, by_trend, forecast_for
from tests.factories import at, make_snapshot

BATTERY = BatteryConfig(capacity_wh=16000, reserve_soc=5)


@pytest.mark.parametrize(
    ("soc", "power_w", "expected"),
    [
        (85, 1000, timedelta(hours=12, minutes=50)),
        (55, 2000, timedelta(hours=4)),
        (5, 1000, timedelta()),
        (3, 1000, timedelta()),
        (30, 400, timedelta(hours=10)),
    ],
)
def test_by_power(soc, power_w, expected):
    assert by_power(soc, power_w, BATTERY) == expected


def test_by_power_rounds_to_ten_minutes():
    # 16000 Wh * 50% / 1234 W = 6.483 h = 6 h 29 min -> 6 h 30 min
    assert by_power(55, 1234, BATTERY) == timedelta(hours=6, minutes=30)


def test_by_power_needs_a_discharge():
    assert by_power(80, 0, BATTERY) is None
    assert by_power(80, -500, BATTERY) is None


def test_by_trend():
    # 18 % in 2 h 10 min -> 50 - 5 = 45 % left at 8.3 %/h -> 5 h 25 min -> 5 h 30 min
    assert by_trend(68, at(0), 50, at(0) + timedelta(hours=2, minutes=10), BATTERY) == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize(
    ("start_soc", "soc", "elapsed"),
    [
        (80, 79, timedelta(hours=2)),
        (80, 70, timedelta(minutes=29)),
        (80, 80, timedelta(hours=3)),
        (70, 80, timedelta(hours=3)),
    ],
)
def test_by_trend_needs_enough_data(start_soc, soc, elapsed):
    assert by_trend(start_soc, at(0), soc, at(0) + elapsed, BATTERY) is None


def test_forecast_on_battery_has_both_estimates():
    snapshot = make_snapshot(480, grid=False, soc=60, battery_w=-1100)
    forecast = forecast_for(snapshot, on_battery=True, drain_soc=72, drain_since=at(0), battery=BATTERY)
    assert forecast == Forecast(
        power_w=1100,
        by_power=timedelta(hours=8),
        by_trend=timedelta(hours=9, minutes=10),
        trend_drop=12,
        trend_elapsed=timedelta(hours=2),
    )


def test_forecast_without_enough_trend_data_still_has_the_power_estimate():
    snapshot = make_snapshot(10, grid=False, soc=72, battery_w=-1100)
    forecast = forecast_for(snapshot, on_battery=True, drain_soc=72, drain_since=at(0), battery=BATTERY)
    assert forecast.by_power is not None
    assert forecast.by_trend is None
    assert forecast.trend_drop is None
    assert forecast.trend_elapsed is None


@pytest.mark.parametrize(
    ("on_battery", "battery_w", "battery"),
    [(False, -1100, BATTERY), (True, 0, BATTERY), (True, 800, BATTERY), (True, -1100, None)],
)
def test_no_forecast(on_battery, battery_w, battery):
    snapshot = make_snapshot(10, grid=False, soc=60, battery_w=battery_w)
    assert forecast_for(snapshot, on_battery=on_battery, drain_soc=70, drain_since=at(0), battery=battery) is None


def test_payload():
    forecast = Forecast(1100, timedelta(hours=13, minutes=20), timedelta(hours=9), 12, timedelta(hours=2))
    assert forecast.to_payload() == {
        "power_w": 1100,
        "by_power_s": 48000,
        "by_trend_s": 32400,
        "trend_drop": 12,
        "trend_elapsed_s": 7200,
    }
    assert Forecast(500, None).to_payload()["by_power_s"] is None
