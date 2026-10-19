from datetime import UTC, datetime, timedelta

import pytest

from lxbot.monitor.detector import EventType
from lxbot.schedule.model import DaySchedule, Outage
from lxbot.schedule.parse import parse_schedule
from lxbot.schedule.service import (
    HEALTH_KEY,
    POSTED_KEY,
    SCHEDULE_KEY,
    ScheduleConfig,
    ScheduleService,
    fingerprint,
)
from lxbot.schedule.source import ScheduleSourceError
from tests.schedule_factories import GROUP, KYIV, TODAY, TOMORROW, local, make_document

CONFIG = ScheduleConfig(group=GROUP)
FIVE_MINUTES = timedelta(minutes=5)
TODAY_OFF = {15: "no", 16: "no", 17: "no", 18: "no"}
TODAY_LATER = {16: "no", 17: "no", 18: "no", 19: "no"}
TOMORROW_OFF = {12: "no", 13: "no", 14: "no", 15: "no"}
DOC = make_document({TODAY: TODAY_OFF, TOMORROW: TOMORROW_OFF})
DOC_TODAY_ONLY = make_document({TODAY: TODAY_OFF})
DOC_TODAY_CHANGED = make_document({TODAY: TODAY_LATER, TOMORROW: TOMORROW_OFF})
DOC_TOMORROW_EMPTY = make_document({TODAY: TODAY_OFF, TOMORROW: {}})
# Most of the summer DTEK publishes no days at all.
DOC_EMPTY = make_document({})


def at(hour, minute=0, day=15):
    return local(2026, 10, day, hour, minute)


class Script:
    """Returns the scripted responses in order; the last one repeats."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0
        self.now = None

    async def fetch(self):
        self.calls += 1
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


class Timeline:
    """Serves the latest response published by the current moment, as the real source does."""

    def __init__(self, *versions):
        self.versions = versions
        self.calls = 0
        self.now = None

    async def fetch(self):
        self.calls += 1
        item = [response for since, response in self.versions if since <= self.now][-1]
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


class RecordingSink:
    def __init__(self):
        self.events = []

    async def publish(self, events):
        self.events.extend(events)


async def make_service(db, source):
    sink = RecordingSink()
    service = ScheduleService(source, db, sink, CONFIG, KYIV)
    await service.load()
    return service, sink


async def run(service, source, start, end, step=FIVE_MINUTES):
    moment = start
    while moment <= end:
        source.now = moment
        await service.tick(moment)
        moment += step


async def posted_before(db, *days):
    """As if these days had gone out before the test starts."""
    await db.save_state(POSTED_KEY, {"posted": {day.isoformat(): None for day in days}})


def types(sink):
    return [e.type for e in sink.events]


def updates(sink):
    return [e for e in sink.events if e.type == EventType.SCHEDULE_UPDATE]


def days_of(event):
    return [d.day for d in event.update.days]


async def test_the_first_schedule_goes_out_once_it_settles(db):
    source = Script(DOC)
    service, sink = await make_service(db, source)
    await run(service, source, at(10), at(12))
    assert types(sink) == [EventType.SCHEDULE_UPDATE]
    event = sink.events[0]
    assert event.at == at(10, 20)
    assert (event.update.published, event.update.changed) == ((TODAY, TOMORROW), ())
    assert event.update.days[0].outages == (Outage(at(14), at(18)),)
    assert event.silent is False
    assert event.source_updated is None


async def test_tomorrow_goes_out_with_today_once_it_settles(db):
    # 9 October: tomorrow appeared at 20:03 and did not change.
    await posted_before(db, TODAY)
    source = Timeline((at(0), DOC_TODAY_ONLY), (at(20, 3), DOC))
    service, sink = await make_service(db, source)
    await run(service, source, at(19), at(21))
    [event] = updates(sink)
    assert event.at == at(20, 25)
    assert event.update.published == (TOMORROW,)
    assert days_of(event) == [TODAY, TOMORROW]


async def test_a_schedule_published_after_midnight_goes_out_quietly_as_today(db):
    # 16 October: nothing all evening, the schedule for the 17th appeared at 00:38.
    await posted_before(db, TODAY)
    source = Timeline((at(0), DOC_TODAY_ONLY), (at(0, 38, day=16), make_document({TOMORROW: TOMORROW_OFF})))
    service, sink = await make_service(db, source)
    await run(service, source, at(19), at(10, day=16))
    [event] = updates(sink)
    assert event.at == at(1, 0, day=16)
    assert event.silent is True
    assert event.update.published == (TOMORROW,)
    assert days_of(event) == [TOMORROW]


async def test_an_empty_day_filled_minutes_later_goes_out_once(db):
    # 17 October: an empty day at 21:03, the real one at 21:18.
    await posted_before(db, TODAY)
    source = Timeline((at(0), DOC_TODAY_ONLY), (at(21, 3), DOC_TOMORROW_EMPTY), (at(21, 18), DOC))
    service, sink = await make_service(db, source)
    await run(service, source, at(20), at(22, 30))
    [event] = updates(sink)
    assert event.at == at(21, 40)
    assert event.update.days[1].outages == (Outage(at(11, day=16), at(15, day=16)),)


async def test_a_change_of_a_posted_day_goes_out_once_it_settles(db):
    await posted_before(db, TODAY, TOMORROW)
    source = Timeline((at(0), DOC), (at(10), DOC_TODAY_CHANGED))
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(12))
    [event] = updates(sink)
    assert event.at == at(10, 20)
    assert (event.update.published, event.update.changed) == ((), (TODAY,))
    assert days_of(event) == [TODAY, TOMORROW]
    assert event.update.days[0].outages == (Outage(at(15), at(19)),)


async def test_a_change_reverted_before_it_settles_goes_nowhere(db):
    await posted_before(db, TODAY, TOMORROW)
    source = Timeline((at(0), DOC), (at(10), DOC_TODAY_CHANGED), (at(10, 10), DOC))
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(12))
    assert sink.events == []


async def test_a_schedule_that_keeps_changing_goes_out_after_45_minutes(db):
    await posted_before(db, TODAY)
    churn = [
        (at(20) + timedelta(minutes=10 * i), make_document({TODAY: TODAY_OFF, TOMORROW: {10 + i: "no"}}))
        for i in range(10)
    ]
    source = Timeline((at(0), DOC_TODAY_ONLY), *churn)
    service, sink = await make_service(db, source)
    await run(service, source, at(19, 30), at(21, 30))
    [event] = updates(sink)
    assert event.at == at(20, 45)


async def test_midnight_does_not_repeat_tomorrow(db):
    await posted_before(db, TODAY, TOMORROW)
    source = Script(DOC)
    service, sink = await make_service(db, source)
    await run(service, source, at(22), at(2, day=16))
    assert sink.events == []


async def test_a_new_day_and_a_change_go_out_in_one_message(db):
    await posted_before(db, TODAY)
    source = Timeline((at(0), DOC_TODAY_ONLY), (at(20), DOC_TODAY_CHANGED))
    service, sink = await make_service(db, source)
    await run(service, source, at(19), at(21))
    [event] = updates(sink)
    assert (event.update.published, event.update.changed) == ((TOMORROW,), (TODAY,))


async def test_a_change_does_not_wait_for_a_new_day_that_has_not_settled(db):
    await posted_before(db, TODAY)
    source = Timeline(
        (at(0), DOC_TODAY_ONLY),
        (at(20), make_document({TODAY: TODAY_LATER})),
        (at(20, 15), DOC_TODAY_CHANGED),
    )
    service, sink = await make_service(db, source)
    await run(service, source, at(19), at(21))
    changed, published = updates(sink)
    assert (changed.at, changed.update.changed, days_of(changed)) == (at(20, 20), (TODAY,), [TODAY])
    assert (published.at, published.update.published, days_of(published)) == (
        at(20, 35),
        (TOMORROW,),
        [TODAY, TOMORROW],
    )


async def test_night_updates_are_silent_and_day_ones_are_not(db):
    await posted_before(db, TODAY, TOMORROW)
    night = make_document({TODAY: TODAY_LATER, TOMORROW: {**TOMORROW_OFF, 20: "no"}})
    source = Timeline((at(0), DOC), (at(10), DOC_TODAY_CHANGED), (at(23, 30), night))
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(23, 55))
    assert [(e.at, e.silent) for e in updates(sink)] == [(at(10, 20), False), (at(23, 50), True)]


async def test_the_source_is_polled_every_five_minutes(db):
    source = Script(DOC)
    service, _ = await make_service(db, source)
    await run(service, source, at(15), at(15, 30), step=timedelta(minutes=1))
    assert source.calls == 7


async def test_an_empty_document_means_no_outages_and_posts_nothing(db):
    source = Script(DOC_EMPTY)
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(9, day=16), step=timedelta(minutes=15))
    assert sink.events == []
    assert service.current(at(12)).day(TODAY) == DaySchedule(GROUP, TODAY)
    assert service.current(at(9, day=16)).timeline() == ()


async def test_outages_after_a_quiet_period_go_out(db):
    source = Timeline((at(0), DOC_EMPTY), (at(20), DOC))
    service, sink = await make_service(db, source)
    await run(service, source, at(19), at(21))
    [event] = updates(sink)
    assert event.at == at(20, 20)
    assert event.update.published == (TODAY, TOMORROW)


async def test_a_cancelled_schedule_goes_out_as_a_change(db):
    await posted_before(db, TODAY, TOMORROW)
    source = Timeline((at(0), DOC), (at(10), DOC_EMPTY))
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(11))
    [event] = updates(sink)
    # An empty document must hold for 45 min: a blank page at the source would otherwise cancel everything.
    assert event.at == at(10, 45)
    assert event.update.changed == (TODAY, TOMORROW)
    assert [d.outages for d in event.update.days] == [(), ()]


async def test_a_short_edit_of_a_posted_day_does_not_ride_along_with_a_new_day(db):
    await posted_before(db, TODAY)
    source = Timeline((at(0), DOC_TODAY_ONLY), (at(20), DOC), (at(20, 15), DOC_TODAY_CHANGED), (at(20, 25), DOC))
    service, sink = await make_service(db, source)
    await run(service, source, at(19), at(21, 30))
    [event] = updates(sink)
    assert event.at == at(20, 25)
    assert (event.update.published, event.update.changed) == ((TOMORROW,), ())
    assert event.update.days[0].outages == (Outage(at(14), at(18)),)


async def test_a_day_that_drops_out_settles_afresh_when_it_returns(db):
    await posted_before(db, TODAY)
    source = Timeline(
        (at(0), DOC_TODAY_ONLY),
        (at(20), DOC_TOMORROW_EMPTY),
        (at(20, 10), DOC_TODAY_ONLY),
        (at(21), DOC_TOMORROW_EMPTY),
        (at(21, 15), DOC),
    )
    service, sink = await make_service(db, source)
    await run(service, source, at(19), at(22, 30))
    [event] = updates(sink)
    assert event.at == at(21, 35)
    assert event.update.days[1].outages == (Outage(at(11, day=16), at(15, day=16)),)


async def test_no_repeat_after_a_restart(db):
    first_source = Script(DOC)
    first, first_sink = await make_service(db, first_source)
    await run(first, first_source, at(10), at(10, 30))
    second_source = Script(DOC)
    second, second_sink = await make_service(db, second_source)
    await run(second, second_source, at(10, 35), at(12))
    assert len(updates(first_sink)) == 1
    assert second_sink.events == []


async def test_the_digest_row_of_the_fixed_time_version_is_migrated_without_repeats(db):
    # The row as the 09:00 / 22:00 digests left it on 2026-10-19: today announced, tomorrow not yet.
    told = fingerprint(parse_schedule(DOC, GROUP, KYIV).day(TODAY))
    await db.save_state(POSTED_KEY, {"today": "2026-10-15", "pending": None, "announced": {"2026-10-15": told}})
    source = Script(DOC)
    service, sink = await make_service(db, source)
    await run(service, source, at(10), at(11))
    [event] = updates(sink)
    assert (event.update.published, event.update.changed) == ((TOMORROW,), ())
    assert set(await db.load_state(POSTED_KEY)) == {"posted"}


async def test_the_migrated_row_posts_nothing_while_only_today_is_known(db):
    told = fingerprint(parse_schedule(DOC_TODAY_ONLY, GROUP, KYIV).day(TODAY))
    await db.save_state(POSTED_KEY, {"today": "2026-10-15", "pending": None, "announced": {"2026-10-15": told}})
    source = Script(DOC_TODAY_ONLY)
    service, sink = await make_service(db, source)
    await run(service, source, at(10), at(23, 55))
    assert sink.events == []


@pytest.mark.parametrize(
    ("pending", "expected"),
    [(None, []), ("2026-10-16", [(TOMORROW,)])],
)
async def test_an_older_digest_row_is_adopted_silently(db, pending, expected):
    # Rows written by the fixed 21:00 digest have only dates; "tomorrow" equal to "pending" was not sent yet.
    await db.save_state(POSTED_KEY, {"today": "2026-10-15", "tomorrow": "2026-10-16", "pending": pending})
    source = Script(DOC)
    service, sink = await make_service(db, source)
    await run(service, source, at(21), at(23))
    assert [e.update.published for e in updates(sink)] == expected


async def test_a_failed_fetch_does_not_mark_fresh_data_as_old(db):
    await posted_before(db, TODAY)
    before = make_document({TODAY: TODAY_OFF}, last_updated=at(18, 58))
    fresh = make_document({TODAY: TODAY_OFF, TOMORROW: TOMORROW_OFF}, last_updated=at(19, 58))
    source = Timeline((at(0), before), (at(20), fresh), (at(20, 10), ScheduleSourceError("blip")))
    service, sink = await make_service(db, source)
    await run(service, source, at(19), at(20, 30))
    [event] = updates(sink)
    assert event.source_updated is None


async def test_an_update_from_old_data_says_how_old_it_is(db):
    source = Script(make_document({TODAY: TODAY_OFF, TOMORROW: TOMORROW_OFF}, last_updated=at(8)))
    service, sink = await make_service(db, source)
    await run(service, source, at(10), at(10, 30))
    [event] = updates(sink)
    assert event.source_updated == at(8)


async def test_not_modified_keeps_the_schedule_fresh(db):
    source = Script(DOC, None)
    service, _ = await make_service(db, source)
    await run(service, source, at(9), at(14), step=timedelta(hours=5))
    assert service.current(at(19, 30)).day(TODAY).outages == (Outage(at(14), at(18)),)


async def test_refresh_failure_keeps_the_last_schedule_until_it_is_stale(db):
    source = Script(DOC, ScheduleSourceError("down"))
    service, _ = await make_service(db, source)
    await run(service, source, at(9), at(9, 15), step=timedelta(minutes=15))
    assert service.current(at(9, 16)) is not None
    assert service.current(at(15, 0)) is not None
    assert service.current(at(15, 1)) is None


async def test_schedule_survives_a_restart_without_network(db):
    first_source = Script(DOC)
    first, _ = await make_service(db, first_source)
    await run(first, first_source, at(8, 30), at(8, 30))
    source = Script(ScheduleSourceError("down"))
    second, sink = await make_service(db, source)
    assert second.current(at(8, 40)).day(TODAY) == first.current(at(8, 40)).day(TODAY)
    await run(second, source, at(9), at(9))
    assert types(sink) == [EventType.SCHEDULE_UPDATE]


async def test_no_schedule_no_update(db):
    source = Script(ScheduleSourceError("down"))
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(9, 30))
    assert sink.events == []
    assert service.current(at(9, 30)) is None


async def test_source_down_after_an_hour_then_up(db):
    source = Timeline((at(0), ScheduleSourceError("down")), (at(10, 10), DOC))
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(9, 55))
    assert sink.events == []
    await run(service, source, at(10), at(10, 30))
    assert types(sink) == [EventType.SCHEDULE_SOURCE_DOWN, EventType.SCHEDULE_SOURCE_UP, EventType.SCHEDULE_UPDATE]
    down, up, update = sink.events
    assert (down.at, up.at, update.at) == (at(9), at(10, 10), at(10, 30))


async def test_source_reporting_not_ok_counts_as_down(db):
    source = Script(make_document({TODAY: TODAY_OFF}, ok=False))
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(10), step=timedelta(hours=1))
    assert EventType.SCHEDULE_SOURCE_DOWN in types(sink)


async def test_stale_source_counts_as_down(db):
    source = Script(make_document({TODAY: TODAY_OFF}, last_updated=datetime(2026, 10, 14, 0, 0, tzinfo=UTC)))
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(10), step=timedelta(hours=1))
    assert EventType.SCHEDULE_SOURCE_DOWN in types(sink)


async def test_run_survives_an_unexpected_error(db):
    class BrokenOnce:
        calls = 0

        async def fetch(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bug")
            return DOC

        async def close(self):
            pass

    times = iter([at(9, 0), at(9, 5), at(9, 30)])

    def clock():
        moment = next(times, None)
        if moment is None:
            raise SystemExit
        return moment

    sink = RecordingSink()
    service = ScheduleService(BrokenOnce(), db, sink, CONFIG, KYIV, clock=clock, tick_seconds=0)
    try:
        await service.run()
    except SystemExit:
        pass
    assert types(sink) == [EventType.SCHEDULE_UPDATE]


async def test_not_modified_after_a_parse_failure_still_counts_as_down(db):
    unparsable = {"lastUpdated": "2026-10-15T00:00:00Z"}
    source = Script(DOC, unparsable, None)
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(15, 15), step=timedelta(minutes=15))
    assert types(sink) == [EventType.SCHEDULE_UPDATE, EventType.SCHEDULE_SOURCE_DOWN]
    assert sink.events[1].at == at(9, 15)
    assert service.current(at(14, 59)) is not None
    assert service.current(at(15, 1)) is None


async def test_source_down_is_remembered_across_a_restart(db):
    down = ScheduleSourceError("down")
    first_source = Script(down)
    first, first_sink = await make_service(db, first_source)
    await run(first, first_source, at(9), at(10), step=timedelta(hours=1))
    assert types(first_sink) == [EventType.SCHEDULE_SOURCE_DOWN]
    second_source = Script(down)
    second, second_sink = await make_service(db, second_source)
    await run(second, second_source, at(10, 15), at(10, 15))
    assert second_sink.events == []
    third_source = Script(DOC)
    third, third_sink = await make_service(db, third_source)
    await run(third, third_source, at(10, 30), at(10, 30))
    assert types(third_sink) == [EventType.SCHEDULE_SOURCE_UP]


async def test_frozen_source_is_not_presented_as_current(db):
    # Updated at 09:00 local, then only 304s: fetched_at stays fresh while the data ages.
    frozen = make_document({TODAY: TODAY_OFF, TOMORROW: TOMORROW_OFF}, last_updated=at(9))
    source = Script(frozen, None)
    service, sink = await make_service(db, source)
    await run(service, source, at(8), at(16), step=timedelta(minutes=15))
    assert service.current(at(16, 0)) is not None
    await run(service, source, at(16, 15), at(12, 0, day=17), step=timedelta(minutes=15))
    assert service.current(at(16, 15)) is None
    assert service.current(at(10, 0, day=17)) is None
    assert types(sink) == [EventType.SCHEDULE_UPDATE, EventType.SCHEDULE_SOURCE_DOWN]
    assert sink.events[1].at == at(9)


async def test_schedule_without_today_is_not_current(db):
    source = Script(make_document({TOMORROW: TOMORROW_OFF}))
    service, _ = await make_service(db, source)
    await run(service, source, at(20), at(20))
    assert service.current(at(20, 0)) is None
    assert service.current(at(0, 30, day=16)) is not None


async def test_an_older_copy_of_the_document_is_ignored(db):
    # CDN edges can serve the previous document after the new one; each flip must not read as a change.
    older = make_document({TODAY: TODAY_OFF}, last_updated=datetime(2026, 10, 16, 22, tzinfo=UTC))
    newer = make_document({TODAY: TODAY_LATER}, last_updated=datetime(2026, 10, 16, 23, tzinfo=UTC))
    source = Script(older, *[newer, older] * 5)
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(11, 30), step=timedelta(minutes=15))
    [event] = updates(sink)
    assert event.update.days[0].outages == (Outage(at(15), at(19)),)
    assert service.current(at(11, 30)).day(TODAY).outages == (Outage(at(15), at(19)),)


async def test_source_down_names_the_time_the_data_stopped(db):
    source = Script(make_document({TODAY: TODAY_OFF}, last_updated=at(9)), None)
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(11), step=timedelta(minutes=1))
    assert types(sink) == [EventType.SCHEDULE_UPDATE]
    await run(service, source, at(11, 1), at(11, 1))
    assert types(sink) == [EventType.SCHEDULE_UPDATE, EventType.SCHEDULE_SOURCE_DOWN]
    assert sink.events[1].at == at(9)


async def test_not_modified_ticks_rarely_write_to_the_card(db, monkeypatch):
    writes = []
    save_state = db.save_state

    async def counting_save_state(key, value):
        writes.append(key)
        await save_state(key, value)

    monkeypatch.setattr(db, "save_state", counting_save_state)
    source = Script(DOC, None)
    service, _ = await make_service(db, source)
    await run(service, source, at(9), at(11, 30), step=timedelta(minutes=1))
    assert writes.count(SCHEDULE_KEY) <= 3
    assert writes.count(POSTED_KEY) == 1


async def test_a_moving_last_updated_alone_rarely_writes_to_the_card(db, monkeypatch):
    writes = []
    save_state = db.save_state

    async def counting_save_state(key, value):
        writes.append(key)
        await save_state(key, value)

    monkeypatch.setattr(db, "save_state", counting_save_state)
    scrapes = [make_document({TODAY: TODAY_OFF}, last_updated=at(9) + FIVE_MINUTES * i) for i in range(31)]
    source = Script(*scrapes)
    service, _ = await make_service(db, source)
    await run(service, source, at(9), at(11, 30))
    assert writes.count(SCHEDULE_KEY) <= 3


async def test_restart_keeps_the_fetch_time_within_an_hour(db):
    first_source = Script(DOC, None)
    first, _ = await make_service(db, first_source)
    await run(first, first_source, at(9), at(9, 45), step=timedelta(minutes=15))
    second, _ = await make_service(db, Script(ScheduleSourceError("down")))
    assert second.current(at(15, 0)) is not None
    assert second.current(at(15, 1)) is None


async def test_a_broken_schedule_row_starts_without_a_schedule(db, caplog):
    await db.save_state(SCHEDULE_KEY, {"group": GROUP, "days": "garbage"})
    source = Script(DOC)
    service, sink = await make_service(db, source)
    assert service.current(at(8, 0)) is None
    assert "stored schedule" in caplog.text
    await run(service, source, at(9), at(9, 30))
    assert types(sink) == [EventType.SCHEDULE_UPDATE]


@pytest.mark.parametrize(
    ("key", "row"),
    [
        (POSTED_KEY, {"today": "not a date"}),
        (POSTED_KEY, {"posted": ["2026-10-15"]}),
        (POSTED_KEY, {"announced": ["2026-10-15"]}),
        (HEALTH_KEY, {"failing_since": 7}),
        (HEALTH_KEY, [1, 2]),
    ],
)
async def test_a_broken_state_row_starts_that_part_empty(db, key, row):
    await db.save_state(key, row)
    source = Script(DOC)
    service, sink = await make_service(db, source)
    await run(service, source, at(9), at(9, 30))
    assert types(sink) == [EventType.SCHEDULE_UPDATE]
