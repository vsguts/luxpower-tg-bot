from lxbot.monitor.detector import (
    Audience,
    Event,
    EventType,
    State,
    Thresholds,
    detect,
)
from tests.factories import at, make_snapshot

T = Thresholds()


def run(snapshots, state=None):
    events = []
    for snapshot in snapshots:
        state, new = detect(state, snapshot, T)
        events += new
    return state, events


def polls(first, count, **kwargs):
    return [make_snapshot(first + i, **kwargs) for i in range(count)]


def types(events):
    return [event.type for event in events]


def test_first_snapshot_is_silent():
    state, events = run([make_snapshot(0)])
    assert events == []
    assert state.grid.value is True
    assert state.gen.value is False


def test_outage_is_reported_from_its_first_poll():
    _, events = run([make_snapshot(0)] + polls(1, 3, grid=False))
    assert events == [Event(EventType.GRID_LOST, at(1), 80, previous_duration=at(1) - at(0))]


def test_flapping_under_the_debounce_is_ignored():
    pattern = [True, False, False, True, False, False, True]
    _, events = run([make_snapshot(i, grid=g) for i, g in enumerate(pattern)])
    assert events == []


def test_restore_while_generator_runs_says_generator_can_stop():
    snapshots = (
        [make_snapshot(0)]
        + polls(1, 3, grid=False)
        + polls(4, 3, grid=False, gen=True)
        + polls(7, 3, grid=True, gen=True)
    )
    _, events = run(snapshots)
    assert types(events) == [EventType.GRID_LOST, EventType.GEN_STARTED, EventType.GRID_RESTORED]
    restored = events[-1]
    assert restored.at == at(7)
    assert restored.previous_duration == at(7) - at(1)
    assert restored.gen_was_running is True


def test_restore_without_generator():
    _, events = run([make_snapshot(0)] + polls(1, 3, grid=False) + polls(4, 3))
    assert events[-1].type == EventType.GRID_RESTORED
    assert events[-1].gen_was_running is False


def test_low_battery_fires_once_and_rearms():
    socs = [31, 30, 29, 35, 30]
    _, events = run([make_snapshot(i, grid=False, soc=s) for i, s in enumerate(socs)])
    assert events == [
        Event(EventType.BATTERY_LOW, at(1), 30),
        Event(EventType.BATTERY_LOW, at(4), 30),
    ]


def test_crossing_both_thresholds_sends_only_critical():
    socs = [40, 14, 13, 19]
    _, events = run([make_snapshot(i, grid=False, soc=s) for i, s in enumerate(socs)])
    assert events == [Event(EventType.BATTERY_CRITICAL, at(1), 14)]


def test_critical_rearms_at_twenty():
    socs = [16, 15, 20, 15]
    _, events = run([make_snapshot(i, grid=False, soc=s) for i, s in enumerate(socs)])
    assert types(events) == [EventType.BATTERY_LOW, EventType.BATTERY_CRITICAL, EventType.BATTERY_CRITICAL]


def test_battery_already_low_when_the_grid_goes():
    _, events = run([make_snapshot(0, soc=25)] + polls(1, 3, grid=False, soc=25))
    assert events == [
        Event(EventType.GRID_LOST, at(1), 25, previous_duration=at(1) - at(0)),
        Event(EventType.BATTERY_LOW, at(3), 25),
    ]


def test_first_start_off_grid_with_low_battery_alerts():
    _, events = run([make_snapshot(0, grid=False, soc=20)])
    assert types(events) == [EventType.BATTERY_LOW]


def test_no_low_alert_while_generator_runs():
    _, events = run(polls(0, 3, grid=False, gen=True, soc=25))
    assert events == []


def test_gen_charged_once_per_generator_run():
    snapshots = (
        [make_snapshot(0, grid=False, gen=True, soc=89)]
        + [make_snapshot(1, grid=False, gen=True, soc=90)]
        + [make_snapshot(2, grid=False, gen=True, soc=91)]
        + polls(3, 3, grid=False, gen=False, soc=91)
        + polls(6, 3, grid=False, gen=True, soc=95)
    )
    _, events = run(snapshots)
    assert types(events) == [
        EventType.GEN_CHARGED,
        EventType.GEN_STOPPED,
        EventType.GEN_STARTED,
        EventType.GEN_CHARGED,
    ]
    assert events[0] == Event(EventType.GEN_CHARGED, at(1), 90)


def test_no_gen_charged_while_grid_is_present():
    _, events = run([make_snapshot(0, grid=True, gen=True, soc=95)])
    assert events == []


def test_state_round_trips_through_a_dict():
    state, _ = run([make_snapshot(0)] + polls(1, 2, grid=False))
    assert state.grid.pending_count == 2
    assert State.from_dict(state.to_dict()) == state


def test_restart_continues_from_stored_state():
    snapshots = [make_snapshot(0)] + polls(1, 3, grid=False) + polls(4, 3)
    _, uninterrupted = run(snapshots)
    state, first_half = run(snapshots[:3])
    _, second_half = run(snapshots[3:], State.from_dict(state.to_dict()))
    assert first_half + second_half == uninterrupted


def test_audience():
    assert Event(EventType.GRID_LOST, at(0)).audience == Audience.SUBSCRIBERS
    assert Event(EventType.GEN_STARTED, at(0)).audience == Audience.JOURNAL
    assert Event(EventType.DONGLE_DOWN, at(0)).audience == Audience.ADMIN


def test_payload():
    event = Event(EventType.GRID_RESTORED, at(4), 70, previous_duration=at(4) - at(0), gen_was_running=True)
    assert event.to_payload() == {"soc": 70, "previous_duration_s": 60, "gen_was_running": True}
