from dataclasses import replace
from datetime import timedelta

from lxbot.monitor.detector import EventType, State, Thresholds, detect
from lxbot.monitor.forecast import BatteryConfig
from tests.factories import at, make_snapshot

BATTERY = BatteryConfig(capacity_wh=16000, reserve_soc=5)
T = Thresholds(battery=BATTERY)


def run(snapshots, state=None, thresholds=T):
    events = []
    for snapshot in snapshots:
        state, new = detect(state, snapshot, thresholds)
        events += new
    return state, events


def off(first, count, soc=80, **kwargs):
    return [make_snapshot(first + i, grid=False, soc=soc, battery_w=-1000, **kwargs) for i in range(count)]


def test_outage_starts_the_battery_stretch_and_forecasts_by_power():
    state, events = run([make_snapshot(0)] + off(1, 3, soc=85))
    assert (state.drain_soc, state.drain_since) == (85, at(3))
    (lost,) = events
    assert lost.type == EventType.GRID_LOST
    assert lost.forecast.power_w == 1000
    assert lost.forecast.by_power == timedelta(hours=12, minutes=50)
    assert lost.forecast.by_trend is None


def test_low_battery_alert_carries_the_trend():
    # 85 % at the outage start, 30 % two hours later (480 polls of 15 s).
    state, _ = run([make_snapshot(0)] + off(1, 3, soc=85))
    state, events = run(off(481, 2, soc=30), state)
    (low,) = events
    assert low.type == EventType.BATTERY_LOW
    assert low.forecast.trend_drop == 55
    assert low.forecast.by_trend is not None


def test_restore_ends_the_battery_stretch():
    state, _ = run([make_snapshot(0)] + off(1, 3) + [make_snapshot(i) for i in (4, 5, 6)])
    assert state.drain_soc is None
    assert state.drain_since is None


def test_generator_pauses_the_stretch_and_its_stop_restarts_it():
    state, _ = run([make_snapshot(0)] + off(1, 3, soc=70))
    gen_on = [make_snapshot(4 + i, grid=False, gen=True, soc=70, battery_w=1500) for i in range(3)]
    state, _ = run(gen_on, state)
    assert state.drain_since is None
    state, _ = run(off(7, 3, soc=90), state)
    assert (state.drain_soc, state.drain_since) == (90, at(9))


def test_first_start_on_battery_starts_the_stretch():
    state, _ = run(off(0, 1, soc=55))
    assert (state.drain_soc, state.drain_since) == (55, at(0))


def test_no_forecast_without_a_battery_config():
    _, events = run([make_snapshot(0)] + off(1, 3), thresholds=Thresholds())
    assert events[0].forecast is None


def test_no_forecast_while_the_battery_is_not_discharging():
    snapshots = [make_snapshot(0)] + [make_snapshot(i, grid=False, battery_w=0) for i in (1, 2, 3)]
    _, events = run(snapshots)
    assert events[0].forecast is None


def test_state_round_trip_and_old_rows():
    state, _ = run([make_snapshot(0)] + off(1, 3))
    assert State.from_dict(state.to_dict()) == state
    old = state.to_dict()
    del old["drain_soc"], old["drain_since"]
    assert State.from_dict(old) == replace(state, drain_soc=None, drain_since=None)


def test_payload_includes_the_forecast_only_when_set():
    _, events = run([make_snapshot(0)] + off(1, 3))
    assert events[0].to_payload()["forecast"]["power_w"] == 1000
    _, plain = run([make_snapshot(0)] + off(1, 3), thresholds=Thresholds())
    assert "forecast" not in plain[0].to_payload()


def test_a_rising_charge_restarts_the_stretch_at_its_peak():
    # Solar lifts the battery during the outage: the trend must not average the climb in.
    state, _ = run([make_snapshot(0)] + off(1, 3, soc=60))
    sunny = [make_snapshot(4 + i, grid=False, soc=60 + i, battery_w=800) for i in range(31)]
    state, _ = run(sunny, state)
    assert (state.drain_soc, state.drain_since) == (90, at(34))
    state, _ = run(off(35, 1, soc=89), state)
    assert (state.drain_soc, state.drain_since) == (90, at(34))


def test_an_old_row_restored_mid_outage_starts_the_stretch_now():
    state, _ = run([make_snapshot(0)] + off(1, 3, soc=90))
    old = state.to_dict()
    del old["drain_soc"], old["drain_since"]
    state, _ = run(off(720, 1, soc=50), State.from_dict(old))
    assert (state.drain_soc, state.drain_since) == (50, at(720))
