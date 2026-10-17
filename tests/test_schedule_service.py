from datetime import UTC, datetime, timedelta

import pytest

from lxbot.monitor.detector import EventType
from lxbot.schedule.model import Outage
from lxbot.schedule.service import DIGESTS_KEY, HEALTH_KEY, SCHEDULE_KEY, ScheduleConfig, ScheduleService
from lxbot.schedule.source import ScheduleSourceError
from tests.schedule_factories import GROUP, KYIV, TODAY, TOMORROW, local, make_document

CONFIG = ScheduleConfig(group=GROUP)
TODAY_OFF = {15: "no", 16: "no", 17: "no", 18: "no"}
TOMORROW_OFF = {12: "no", 13: "no", 14: "no", 15: "no"}
DOC = make_document({TODAY: TODAY_OFF, TOMORROW: TOMORROW_OFF})
DOC_TODAY_ONLY = make_document({TODAY: TODAY_OFF})
DOC_TODAY_CHANGED = make_document({TODAY: {16: "no", 17: "no", 18: "no", 19: "no"}, TOMORROW: TOMORROW_OFF})
DOC_TOMORROW_CHANGED = make_document({TODAY: TODAY_OFF, TOMORROW: {13: "no"}})
DOC_TOMORROW_EMPTY = make_document({TODAY: TODAY_OFF, TOMORROW: {}})


class FakeSource:
    """Returns the scripted responses in order; the last one repeats."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
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


async def make_service(db, *responses):
    sink = RecordingSink()
    source = FakeSource(*responses)
    service = ScheduleService(source, db, sink, CONFIG, KYIV)
    await service.load()
    return service, sink, source


def types(sink):
    return [e.type for e in sink.events]


def at(hour, minute=0, day=15):
    return local(2026, 10, day, hour, minute)


async def test_morning_digest_once_at_nine(db):
    service, sink, _ = await make_service(db, DOC)
    await service.tick(at(8, 59))
    assert sink.events == []
    await service.tick(at(9, 0))
    await service.tick(at(9, 1))
    assert types(sink) == [EventType.SCHEDULE_TODAY]
    digest = sink.events[0]
    assert digest.at == at(9, 0)
    assert digest.schedule.outages == (Outage(at(14), at(18)),)


async def test_late_start_still_sends_the_morning_digest(db):
    service, sink, _ = await make_service(db, DOC)
    await service.tick(at(11, 0))
    assert types(sink) == [EventType.SCHEDULE_TODAY]


async def test_no_resend_after_restart(db):
    first, sink, _ = await make_service(db, DOC)
    await first.tick(at(9, 0))
    second, second_sink, _ = await make_service(db, DOC)
    await second.tick(at(9, 5))
    assert types(sink) == [EventType.SCHEDULE_TODAY]
    assert second_sink.events == []


async def test_evening_digest_for_tomorrow_goes_out_once_it_settles(db):
    # 9 October: tomorrow appeared at 20:03 and did not change.
    service, sink, _ = await make_service(db, DOC_TODAY_ONLY, DOC_TODAY_ONLY, DOC)
    await tick_through(service, at(19, 50), at(20, 15), step=timedelta(minutes=5))
    assert sink.events == []
    await tick_through(service, at(20, 20), at(20, 30), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW]
    assert sink.events[0].at == at(20, 20)
    assert sink.events[0].schedule.day == TOMORROW
    assert sink.events[0].silent is False


async def test_tomorrow_pending_at_ten_then_published(db):
    service, sink, _ = await make_service(db, *[DOC_TODAY_ONLY] * 13, DOC)
    await tick_through(service, at(21, 0), at(22, 0), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW_PENDING]
    assert sink.events[0].at == at(22, 0)
    await tick_through(service, at(22, 5), at(22, 40), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW_PENDING, EventType.SCHEDULE_TOMORROW_PUBLISHED]
    assert sink.events[1].schedule.day == TOMORROW


async def test_published_after_midnight_goes_out_as_today_quietly(db):
    # 16 October: nothing by 22:00, the schedule for the 17th appeared at 00:38.
    after_midnight = make_document({TOMORROW: TOMORROW_OFF})
    # 21:30-00:35 is 38 polls at the evening's 5-minute pace.
    service, sink, _ = await make_service(db, *[DOC_TODAY_ONLY] * 38, after_midnight)
    await tick_through(service, at(21, 30), at(0, 35, day=16), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW_PENDING]
    await tick_through(service, at(0, 40, day=16), at(9, 30, day=16), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW_PENDING, EventType.SCHEDULE_TODAY]
    published = sink.events[1]
    assert published.at == at(1, 0, day=16)
    assert published.schedule.day == TOMORROW
    assert published.silent is True


async def test_change_after_the_morning_digest_is_announced_once_settled(db):
    service, sink, _ = await make_service(db, DOC, DOC_TODAY_CHANGED)
    await service.tick(at(9, 0))
    await tick_through(service, at(9, 15), at(9, 30))
    assert types(sink) == [EventType.SCHEDULE_TODAY]
    await service.tick(at(9, 45))
    assert types(sink) == [EventType.SCHEDULE_TODAY, EventType.SCHEDULE_CHANGED]
    assert sink.events[1].schedule.outages == (Outage(at(15), at(19)),)


async def test_change_before_the_digest_is_silent_and_the_digest_has_it(db):
    service, sink, _ = await make_service(db, DOC, DOC_TODAY_CHANGED)
    await service.tick(at(8, 30))
    await service.tick(at(8, 45))
    await service.tick(at(9, 0))
    assert types(sink) == [EventType.SCHEDULE_TODAY]
    assert sink.events[0].schedule.outages == (Outage(at(15), at(19)),)


async def test_tomorrow_change_after_the_evening_digest_is_announced(db):
    service, sink, _ = await make_service(db, *[DOC] * 13, DOC_TOMORROW_CHANGED)
    await tick_through(service, at(18, 0), at(19, 0), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW]
    await tick_through(service, at(19, 5), at(19, 30), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW, EventType.SCHEDULE_CHANGED]
    assert sink.events[1].schedule.day == TOMORROW


async def test_placeholder_then_real_schedule_is_announced_once(db):
    # 17 October: an empty day at 21:03, the real one at 21:18.
    service, sink, _ = await make_service(db, DOC_TODAY_ONLY, DOC_TOMORROW_EMPTY, DOC_TOMORROW_EMPTY, DOC_TOMORROW_EMPTY, DOC)
    await tick_through(service, at(21, 0), at(22, 30), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW]
    assert sink.events[0].schedule.outages == (Outage(at(11, day=16), at(15, day=16)),)


async def test_tomorrow_filled_after_the_evening_digest_is_announced(db):
    service, sink, _ = await make_service(db, *[DOC_TOMORROW_EMPTY] * 13, DOC)
    await tick_through(service, at(18, 0), at(19, 0), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW]
    assert sink.events[0].schedule.outages == ()
    await tick_through(service, at(19, 5), at(19, 30), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW, EventType.SCHEDULE_CHANGED]


async def test_a_schedule_that_keeps_changing_is_announced_after_45_minutes(db):
    versions = [make_document({TODAY: TODAY_OFF, TOMORROW: {h: "no"}}) for h in range(10, 20)]
    service, sink, _ = await make_service(db, DOC_TODAY_ONLY, *[v for v in versions for _ in (0, 1)])
    await tick_through(service, at(20, 0), at(21, 0), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW]
    assert sink.events[0].at == at(20, 50)


async def test_the_evening_is_polled_every_five_minutes(db):
    service, _, source = await make_service(db, DOC)
    await tick_through(service, at(15, 0), at(15, 30), step=timedelta(minutes=1))
    assert source.calls == 3
    await tick_through(service, at(18, 0), at(18, 30), step=timedelta(minutes=1))
    assert source.calls == 3 + 7


async def test_night_messages_are_silent_and_day_ones_are_not(db):
    night = make_document({TODAY: TODAY_OFF, TOMORROW: {**TOMORROW_OFF, 20: "no"}})
    service, sink, _ = await make_service(db, *[DOC] * 13, night)
    await tick_through(service, at(18, 0), at(19, 0), step=timedelta(minutes=5))
    await tick_through(service, at(23, 0), at(23, 30), step=timedelta(minutes=5))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW, EventType.SCHEDULE_CHANGED]
    assert [e.silent for e in sink.events] == [False, True]


async def test_recent_fetch_is_reused_for_the_digest(db):
    service, sink, source = await make_service(db, DOC)
    await service.tick(at(8, 50))
    await service.tick(at(9, 0))
    assert source.calls == 1
    assert types(sink) == [EventType.SCHEDULE_TODAY]


async def test_digest_refreshes_an_old_schedule_first(db):
    # 08:50 fails, so at 09:00 the last good fetch is an hour old although an attempt was 10 min ago.
    service, sink, source = await make_service(db, DOC, ScheduleSourceError("blip"), DOC_TODAY_CHANGED)
    await service.tick(at(8, 0))
    await service.tick(at(8, 50))
    await service.tick(at(9, 0))
    assert source.calls == 3
    assert sink.events[-1].type == EventType.SCHEDULE_TODAY
    assert sink.events[-1].schedule.outages == (Outage(at(15), at(19)),)


async def test_not_modified_keeps_the_schedule_fresh(db):
    service, _, _ = await make_service(db, DOC, None)
    await service.tick(at(9, 0))
    await service.tick(at(14, 0))
    assert service.current(at(19, 30)) is not None
    assert service.current(at(19, 30)).day(TODAY).outages == (Outage(at(14), at(18)),)


async def test_refresh_failure_keeps_the_last_schedule_until_it_is_stale(db):
    service, _, _ = await make_service(db, DOC, ScheduleSourceError("down"))
    await service.tick(at(9, 0))
    await service.tick(at(9, 15))
    assert service.current(at(9, 16)) is not None
    assert service.current(at(15, 0)) is not None
    assert service.current(at(15, 1)) is None


async def test_schedule_survives_a_restart_without_network(db):
    first, _, _ = await make_service(db, DOC)
    await first.tick(at(8, 30))
    second, sink, _ = await make_service(db, ScheduleSourceError("down"))
    assert second.current(at(8, 40)).day(TODAY) == first.current(at(8, 40)).day(TODAY)
    await second.tick(at(9, 0))
    assert types(sink) == [EventType.SCHEDULE_TODAY]


async def test_no_schedule_no_digest(db):
    service, sink, _ = await make_service(db, ScheduleSourceError("down"))
    await service.tick(at(9, 0))
    assert sink.events == []
    assert service.current(at(9, 0)) is None


async def test_source_down_after_an_hour_then_up(db):
    # With no schedule and a digest due, retries are throttled to 5 min; DOWN comes from the health check at 10:00.
    down = ScheduleSourceError("down")
    service, sink, _ = await make_service(db, down, down, DOC)
    await service.tick(at(9, 0))
    await service.tick(at(9, 59))
    assert sink.events == []
    await service.tick(at(10, 0))
    assert types(sink) == [EventType.SCHEDULE_SOURCE_DOWN]
    assert sink.events[0].at == at(9, 0)
    await service.tick(at(10, 15))
    assert types(sink)[1:] == [EventType.SCHEDULE_SOURCE_UP, EventType.SCHEDULE_TODAY]


async def test_source_reporting_not_ok_counts_as_down(db):
    service, sink, _ = await make_service(db, make_document({TODAY: TODAY_OFF}, ok=False))
    await service.tick(at(9, 0))
    await service.tick(at(10, 0))
    assert EventType.SCHEDULE_SOURCE_DOWN in types(sink)


async def test_stale_source_counts_as_down(db):
    old = make_document({TODAY: TODAY_OFF}, last_updated=datetime(2026, 10, 14, 0, 0, tzinfo=UTC))
    service, sink, _ = await make_service(db, old)
    await service.tick(at(9, 0))
    await service.tick(at(10, 0))
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

    times = iter([at(9, 0), at(9, 15), at(9, 30)])

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
    assert EventType.SCHEDULE_TODAY in types(sink)


async def test_digest_and_change_events_carry_their_day_schedule(db):
    service, sink, _ = await make_service(db, *[DOC_TODAY_ONLY] * 2, *[DOC] * 3, DOC_TOMORROW_CHANGED)
    await service.tick(at(9, 0))
    await tick_through(service, at(22, 0), at(23, 30), step=timedelta(minutes=15))
    assert types(sink) == [
        EventType.SCHEDULE_TODAY,
        EventType.SCHEDULE_TOMORROW_PENDING,
        EventType.SCHEDULE_TOMORROW_PUBLISHED,
        EventType.SCHEDULE_CHANGED,
    ]
    today, pending, published, changed = sink.events
    assert pending.schedule is None
    assert today.schedule.day == TODAY
    assert published.schedule.day == TOMORROW
    assert changed.schedule.day == TOMORROW


async def test_change_during_the_night_after_the_evening_digest_is_announced(db):
    night = make_document({TODAY: TODAY_OFF, TOMORROW: {**TOMORROW_OFF, 20: "no"}})
    service, sink, _ = await make_service(db, *[DOC] * 13, night)
    await tick_through(service, at(18, 0), at(19, 0), step=timedelta(minutes=5))
    await tick_through(service, at(2, 0, day=16), at(2, 30, day=16))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW, EventType.SCHEDULE_CHANGED]
    assert sink.events[1].schedule.day == TOMORROW
    assert sink.events[1].silent is True
    assert sink.events[1].schedule.outages == (
        Outage(at(11, day=16), at(15, day=16)),
        Outage(at(19, day=16), at(20, day=16)),
    )


async def test_not_modified_after_a_parse_failure_still_counts_as_down(db):
    unparsable = {"lastUpdated": "2026-10-15T00:00:00Z"}
    service, sink, _ = await make_service(db, DOC, unparsable, None)
    moment = at(9, 0)
    while moment <= at(15, 15):
        await service.tick(moment)
        moment += timedelta(minutes=15)
    assert types(sink) == [EventType.SCHEDULE_TODAY, EventType.SCHEDULE_SOURCE_DOWN]
    assert sink.events[1].at == at(9, 15)
    assert service.current(at(14, 59)) is not None
    assert service.current(at(15, 1)) is None


async def test_dead_source_is_not_hammered_while_a_digest_is_due(db):
    service, sink, source = await make_service(db, ScheduleSourceError("down"))
    moment = at(9, 0)
    while moment <= at(9, 30):
        await service.tick(moment)
        moment += timedelta(minutes=1)
    assert source.calls <= 7


async def test_source_down_is_remembered_across_a_restart(db):
    down = ScheduleSourceError("down")
    first, first_sink, _ = await make_service(db, down)
    await first.tick(at(9, 0))
    await first.tick(at(10, 0))
    assert types(first_sink) == [EventType.SCHEDULE_SOURCE_DOWN]
    second, second_sink, _ = await make_service(db, down)
    await second.tick(at(10, 15))
    assert second_sink.events == []
    third, third_sink, _ = await make_service(db, DOC)
    await third.tick(at(10, 30))
    assert types(third_sink) == [EventType.SCHEDULE_SOURCE_UP, EventType.SCHEDULE_TODAY]


async def tick_through(service, start, end, step=timedelta(minutes=15)):
    moment = start
    while moment <= end:
        await service.tick(moment)
        moment += step


async def test_frozen_source_is_not_presented_as_current(db):
    # Updated at 09:00 local, then only 304s: fetched_at stays fresh while the data ages.
    frozen = make_document({TODAY: TODAY_OFF, TOMORROW: TOMORROW_OFF}, last_updated=datetime(2026, 10, 15, 6, tzinfo=UTC))
    service, sink, _ = await make_service(db, frozen, None)
    await tick_through(service, at(8, 0), at(16, 0))
    assert service.current(at(16, 0)) is not None
    await tick_through(service, at(16, 15), at(12, 0, day=17))
    assert service.current(at(16, 15)) is None
    assert service.current(at(10, 0, day=17)) is None
    assert types(sink) == [
        EventType.SCHEDULE_TODAY,
        EventType.SCHEDULE_SOURCE_DOWN,
        EventType.SCHEDULE_TOMORROW,
        EventType.SCHEDULE_TODAY,
    ]
    healthy_digest, _, *stale_digests = sink.events
    assert healthy_digest.source_updated is None
    assert [e.source_updated for e in stale_digests] == [at(9, 0), at(9, 0)]


async def test_schedule_without_today_is_not_current(db):
    service, _, _ = await make_service(db, make_document({TOMORROW: TOMORROW_OFF}))
    await service.tick(at(20, 0))
    assert service.current(at(20, 0)) is None
    assert service.current(at(0, 30, day=16)) is not None


async def test_unhealthy_source_without_tomorrow_sends_the_evening_digest_once_it_recovers(db):
    down = ScheduleSourceError("down")
    service, sink, _ = await make_service(db, DOC_TODAY_ONLY, *[down] * 7, DOC)
    await tick_through(service, at(19, 45), at(21, 30))
    assert types(sink) == [EventType.SCHEDULE_SOURCE_DOWN]
    await tick_through(service, at(21, 45), at(22, 15))
    assert types(sink)[1:] == [EventType.SCHEDULE_SOURCE_UP, EventType.SCHEDULE_TOMORROW]
    assert sink.events[-1].source_updated is None


async def test_change_notice_from_an_unhealthy_source_carries_its_age(db):
    healthy = make_document({TODAY: TODAY_OFF}, last_updated=datetime(2026, 10, 15, 5, 45, tzinfo=UTC))
    failing_change = make_document(
        {TODAY: {16: "no", 17: "no", 18: "no", 19: "no"}},
        ok=False,
        last_updated=datetime(2026, 10, 15, 6, 10, tzinfo=UTC),
    )
    service, sink, _ = await make_service(db, healthy, failing_change)
    await tick_through(service, at(9, 0), at(9, 45))
    assert types(sink) == [EventType.SCHEDULE_TODAY, EventType.SCHEDULE_CHANGED]
    assert sink.events[0].source_updated is None
    assert sink.events[1].source_updated == at(9, 10)


async def test_an_older_copy_of_the_document_is_ignored(db):
    # CDN edges can serve the previous document after the new one; each flip must not read as a change.
    older = make_document({TODAY: TODAY_OFF}, last_updated=datetime(2026, 10, 16, 22, tzinfo=UTC))
    newer = make_document({TODAY: {16: "no", 17: "no", 18: "no", 19: "no"}}, last_updated=datetime(2026, 10, 16, 23, tzinfo=UTC))
    service, sink, _ = await make_service(db, older, *[newer, older] * 5)
    await tick_through(service, at(9, 0), at(11, 30))
    assert types(sink) == [EventType.SCHEDULE_TODAY, EventType.SCHEDULE_CHANGED]
    assert service.current(at(11, 30)).day(TODAY).outages == (Outage(at(15), at(19)),)


async def test_source_down_names_the_time_the_data_stopped(db):
    frozen = make_document({TODAY: TODAY_OFF}, last_updated=datetime(2026, 10, 15, 6, tzinfo=UTC))
    service, sink, _ = await make_service(db, frozen, None)
    await tick_through(service, at(9, 0), at(11, 0), step=timedelta(minutes=1))
    assert types(sink) == [EventType.SCHEDULE_TODAY]
    await service.tick(at(11, 1))
    assert types(sink) == [EventType.SCHEDULE_TODAY, EventType.SCHEDULE_SOURCE_DOWN]
    assert sink.events[1].at == at(9, 0)


async def test_not_modified_ticks_rarely_write_to_the_card(db, monkeypatch):
    writes = []
    save_state = db.save_state

    async def counting_save_state(key, value):
        writes.append(key)
        await save_state(key, value)

    monkeypatch.setattr(db, "save_state", counting_save_state)
    service, _, _ = await make_service(db, DOC, None)
    await tick_through(service, at(9, 0), at(11, 30))
    assert writes.count(SCHEDULE_KEY) <= 3
    assert writes.count(DIGESTS_KEY) == 1


async def test_restart_keeps_the_fetch_time_within_an_hour(db):
    first, _, _ = await make_service(db, DOC, None)
    await tick_through(first, at(9, 0), at(9, 45))
    second, _, _ = await make_service(db, ScheduleSourceError("down"))
    assert second.current(at(15, 0)) is not None
    assert second.current(at(15, 1)) is None


async def test_a_broken_schedule_row_starts_without_a_schedule(db, caplog):
    await db.save_state(SCHEDULE_KEY, {"group": GROUP, "days": "garbage"})
    service, sink, _ = await make_service(db, DOC)
    assert service.current(at(8, 0)) is None
    assert "stored schedule" in caplog.text
    await service.tick(at(9, 0))
    assert types(sink) == [EventType.SCHEDULE_TODAY]


@pytest.mark.parametrize(
    ("key", "row"),
    [(DIGESTS_KEY, {"today": "not a date"}), (HEALTH_KEY, {"failing_since": 7}), (HEALTH_KEY, [1, 2])],
)
async def test_a_broken_state_row_starts_that_part_empty(db, key, row):
    await db.save_state(key, row)
    service, sink, _ = await make_service(db, DOC)
    await service.tick(at(9, 0))
    assert types(sink) == [EventType.SCHEDULE_TODAY]


async def test_an_old_digest_row_counts_its_evening_announcement(db):
    # Rows written by the fixed 21:00 digest have "tomorrow" instead of per-day fingerprints.
    await db.save_state(DIGESTS_KEY, {"today": "2026-10-15", "tomorrow": "2026-10-16", "pending": None})
    service, sink, _ = await make_service(db, DOC)
    await tick_through(service, at(21, 0), at(23, 0), step=timedelta(minutes=5))
    assert sink.events == []
    stored = await db.load_state(DIGESTS_KEY)
    assert set(stored) == {"today", "pending", "announced"}
    assert "2026-10-16" in stored["announced"]
