from datetime import timedelta

from lxbot.monitor.detector import Event, EventType
from lxbot.schedule.annotate import ScheduleAnnotator
from lxbot.schedule.compare import MatchKind, ScheduleMatch
from lxbot.schedule.model import DaySchedule, Outage, Schedule
from tests.schedule_factories import GROUP, TODAY, local

MORNING = Outage(local(2026, 10, 15, 11), local(2026, 10, 15, 15))
EVENING = Outage(local(2026, 10, 15, 20, 30), local(2026, 10, 16, 0))
SCHEDULE = Schedule(GROUP, {TODAY: DaySchedule(GROUP, TODAY, (MORNING, EVENING))})
WINDOW = timedelta(minutes=30)


class Provider:
    def __init__(self, schedule):
        self.schedule = schedule
        self.asked = []

    def current(self, now):
        self.asked.append(now)
        return self.schedule


class RecordingSink:
    def __init__(self):
        self.events = []

    async def publish(self, events):
        self.events.extend(events)


async def publish(schedule, *events):
    sink = RecordingSink()
    provider = Provider(schedule)
    await ScheduleAnnotator(sink, provider, WINDOW).publish(list(events))
    return sink.events, provider


async def test_grid_lost_gets_a_match_for_its_own_time():
    lost = Event(EventType.GRID_LOST, local(2026, 10, 15, 11, 18), 80, timedelta(hours=16))
    (annotated,), provider = await publish(SCHEDULE, lost)
    assert annotated.match == ScheduleMatch(MatchKind.LOST, MORNING, timedelta(minutes=18))
    assert provider.asked == [lost.at]
    assert annotated.soc == 80


async def test_grid_restored_gets_the_next_outage():
    restored = Event(EventType.GRID_RESTORED, local(2026, 10, 15, 14, 48), 60, timedelta(hours=3))
    (annotated,), _ = await publish(SCHEDULE, restored)
    assert annotated.match == ScheduleMatch(MatchKind.RESTORED, MORNING, timedelta(minutes=-12), EVENING)


async def test_other_events_pass_through_untouched():
    low = Event(EventType.BATTERY_LOW, local(2026, 10, 15, 12), 30)
    events, provider = await publish(SCHEDULE, low)
    assert events == [low]
    assert provider.asked == []


async def test_unknown_schedule_leaves_grid_events_as_they_are():
    lost = Event(EventType.GRID_LOST, local(2026, 10, 15, 11, 18), 80, timedelta(hours=1))
    events, _ = await publish(None, lost)
    assert events == [lost]


async def test_order_is_kept():
    lost = Event(EventType.GRID_LOST, local(2026, 10, 15, 11, 18), 25, timedelta(hours=1))
    low = Event(EventType.BATTERY_LOW, local(2026, 10, 15, 11, 19), 25)
    events, _ = await publish(SCHEDULE, lost, low)
    assert [e.type for e in events] == [EventType.GRID_LOST, EventType.BATTERY_LOW]


async def test_a_failing_schedule_never_loses_the_grid_alert(caplog):
    class BrokenProvider:
        def current(self, now):
            raise RuntimeError("bug")

    lost = Event(EventType.GRID_LOST, local(2026, 10, 15, 11, 18), 80, timedelta(hours=1))
    sink = RecordingSink()
    await ScheduleAnnotator(sink, BrokenProvider(), WINDOW).publish([lost])
    assert sink.events == [lost]
    assert "schedule annotation failed" in caplog.text
