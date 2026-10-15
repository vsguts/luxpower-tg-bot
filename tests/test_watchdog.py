from datetime import timedelta

from lxbot.monitor.detector import Event, EventType
from lxbot.monitor.watchdog import DongleWatch
from tests.factories import BASE


def test_down_after_five_minutes_then_up():
    watch = DongleWatch()
    assert watch.failure(BASE) == []
    assert watch.failure(BASE + timedelta(minutes=4, seconds=59)) == []
    assert watch.failure(BASE + timedelta(minutes=5)) == [Event(EventType.DONGLE_DOWN, BASE)]
    assert watch.failure(BASE + timedelta(minutes=6)) == []
    up = BASE + timedelta(minutes=7)
    assert watch.success(up) == [Event(EventType.DONGLE_UP, up)]
    assert watch.success(up) == []


def test_short_failure_is_silent():
    watch = DongleWatch()
    watch.failure(BASE)
    assert watch.success(BASE + timedelta(minutes=1)) == []
    assert watch.failure(BASE + timedelta(minutes=2)) == []
    assert watch.failure(BASE + timedelta(minutes=6)) == []
