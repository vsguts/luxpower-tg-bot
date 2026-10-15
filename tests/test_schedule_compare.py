from datetime import timedelta

import pytest

from lxbot.schedule.compare import (
    MatchKind,
    ScheduleMatch,
    current_outage,
    match_lost,
    match_restored,
    next_outage,
)
from lxbot.schedule.model import Outage, merge_outages
from tests.schedule_factories import local

WINDOW = timedelta(minutes=30)
MORNING = Outage(local(2026, 10, 15, 11), local(2026, 10, 15, 15))
POSSIBLE = Outage(local(2026, 10, 15, 18), local(2026, 10, 15, 19), certain=False)
EVENING = Outage(local(2026, 10, 15, 20, 30), local(2026, 10, 16, 0))
TIMELINE = merge_outages([EVENING, MORNING, POSSIBLE])


def at(hour, minute=0):
    return local(2026, 10, 15, hour, minute)


@pytest.mark.parametrize(
    ("hour", "minute", "interval", "deviation"),
    [
        (11, 18, MORNING, timedelta(minutes=18)),
        (10, 40, MORNING, timedelta(minutes=-20)),
        (10, 30, MORNING, timedelta(minutes=-30)),
        (14, 59, MORNING, timedelta(hours=3, minutes=59)),
        (18, 5, POSSIBLE, timedelta(minutes=5)),
        (20, 30, EVENING, timedelta()),
    ],
)
def test_lost_on_schedule(hour, minute, interval, deviation):
    assert match_lost(TIMELINE, at(hour, minute), WINDOW) == ScheduleMatch(MatchKind.LOST, interval, deviation)


@pytest.mark.parametrize(("hour", "minute"), [(10, 29), (15, 0), (16, 0), (8, 0)])
def test_lost_off_schedule(hour, minute):
    assert match_lost(TIMELINE, at(hour, minute), WINDOW) == ScheduleMatch(MatchKind.LOST)


@pytest.mark.parametrize(
    ("hour", "minute", "interval", "deviation", "following"),
    [
        (14, 48, MORNING, timedelta(minutes=-12), POSSIBLE),
        (15, 30, MORNING, timedelta(minutes=30), POSSIBLE),
        (19, 0, POSSIBLE, timedelta(), EVENING),
        (23, 50, EVENING, timedelta(minutes=-10), None),
    ],
)
def test_restored_on_schedule(hour, minute, interval, deviation, following):
    assert match_restored(TIMELINE, at(hour, minute), WINDOW) == ScheduleMatch(
        MatchKind.RESTORED, interval, deviation, following
    )


@pytest.mark.parametrize(("hour", "minute"), [(15, 31), (11, 0), (9, 0)])
def test_restored_off_schedule_still_names_the_next_outage(hour, minute):
    match = match_restored(TIMELINE, at(hour, minute), WINDOW)
    assert match.interval is None
    assert match.deviation is None
    assert match.next_outage == next_outage(TIMELINE, at(hour, minute))


def test_interval_across_midnight():
    timeline = merge_outages(
        [Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 0)), Outage(local(2026, 10, 16, 0), local(2026, 10, 16, 1))]
    )
    night = Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 1))
    assert match_lost(timeline, local(2026, 10, 15, 23, 10), WINDOW).interval == night
    restored = match_restored(timeline, local(2026, 10, 16, 0, 50), WINDOW)
    assert restored.interval == night
    assert restored.deviation == timedelta(minutes=-10)


def test_current_and_next_outage():
    assert current_outage(TIMELINE, at(12)) == MORNING
    assert current_outage(TIMELINE, at(15)) is None
    assert next_outage(TIMELINE, at(12)) == POSSIBLE
    assert next_outage(TIMELINE, at(21)) is None
    assert next_outage((), at(12)) is None


def test_payload():
    match = ScheduleMatch(MatchKind.RESTORED, MORNING, timedelta(minutes=-12), POSSIBLE)
    assert match.to_payload() == {
        "kind": "restored",
        "interval": MORNING.to_list(),
        "deviation_s": -720,
        "next_outage": POSSIBLE.to_list(),
    }
    assert ScheduleMatch(MatchKind.LOST).to_payload() == {
        "kind": "lost",
        "interval": None,
        "deviation_s": None,
        "next_outage": None,
    }
