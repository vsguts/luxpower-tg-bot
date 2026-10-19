import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from lxbot.schedule.model import DaySchedule, Outage
from lxbot.schedule.parse import ScheduleParseError, group_key, parse_schedule
from tests.schedule_factories import GROUP, KYIV, TODAY, TOMORROW, local, make_document, make_slots

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "kyiv-region-sample.json").read_text())
FALL_BACK = date(2026, 10, 25)
SPRING_FORWARD = date(2026, 3, 29)


def test_group_key():
    assert group_key("3.2") == "GPV3.2"


def test_real_document_for_queue_3_2():
    schedule = parse_schedule(SAMPLE, "GPV3.2", KYIV)
    assert schedule.group == "GPV3.2"
    assert schedule.days[TODAY].outages == (Outage(local(2026, 10, 15, 14), local(2026, 10, 15, 18)),)
    assert schedule.days[TOMORROW].outages == (Outage(local(2026, 10, 16, 11), local(2026, 10, 16, 15)),)
    assert schedule.dtek_updated == local(2026, 10, 15, 19, 45)
    assert schedule.source_ok is True
    assert schedule.source_updated == datetime(2026, 10, 15, 20, 31, 23, 68000, tzinfo=UTC)


def test_half_hours_merge_into_intervals():
    schedule = parse_schedule(SAMPLE, "GPV6.2", KYIV)
    assert schedule.days[TODAY].outages == (
        Outage(local(2026, 10, 15, 10, 30), local(2026, 10, 15, 14, 30)),
        Outage(local(2026, 10, 15, 17, 30), local(2026, 10, 15, 21, 30)),
    )
    assert schedule.days[TODAY].certain_total == timedelta(hours=8)


def test_possible_outages_stay_separate_from_certain_ones():
    schedule = parse_schedule(make_document({TODAY: {11: "maybe", 12: "no"}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == (
        Outage(local(2026, 10, 15, 10), local(2026, 10, 15, 11), certain=False),
        Outage(local(2026, 10, 15, 11), local(2026, 10, 15, 12)),
    )


def test_possible_half_hours():
    schedule = parse_schedule(make_document({TODAY: {11: "mfirst", 12: "msecond"}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == (
        Outage(local(2026, 10, 15, 10), local(2026, 10, 15, 10, 30), certain=False),
        Outage(local(2026, 10, 15, 11, 30), local(2026, 10, 15, 12), certain=False),
    )


def test_unknown_state_is_a_possible_outage():
    schedule = parse_schedule(make_document({TODAY: {11: "weird"}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == (Outage(local(2026, 10, 15, 10), local(2026, 10, 15, 11), certain=False),)


def test_a_day_without_outages():
    schedule = parse_schedule(make_document({TODAY: {}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == ()
    assert schedule.days[TODAY].certain_total == timedelta()


def test_timeline_merges_across_midnight():
    schedule = parse_schedule(make_document({TODAY: {24: "no"}, TOMORROW: {1: "no"}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == (Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 0)),)
    assert schedule.timeline() == (Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 1)),)


def test_missing_group_is_an_error():
    with pytest.raises(ScheduleParseError, match="GPV9.9"):
        parse_schedule(SAMPLE, "GPV9.9", KYIV)


def test_document_without_fact_is_an_error():
    with pytest.raises(ScheduleParseError):
        parse_schedule({"regionId": "kyiv-region"}, GROUP, KYIV)


def test_bad_slot_key_is_an_error():
    document = make_document({TODAY: {}})
    next(iter(document["fact"]["data"].values()))[GROUP]["x"] = "no"
    with pytest.raises(ScheduleParseError):
        parse_schedule(document, GROUP, KYIV)


def test_missing_metadata_is_tolerated():
    document = make_document({TODAY: {}})
    del document["lastUpdated"]
    document["fact"]["update"] = "soon"
    schedule = parse_schedule(document, GROUP, KYIV)
    assert schedule.source_updated is None
    assert schedule.dtek_updated is None


def test_day_schedule_round_trip_and_label():
    day = parse_schedule(SAMPLE, "GPV6.2", KYIV).days[TODAY]
    assert DaySchedule.from_dict(day.to_dict()) == day
    assert day.label == "6.2"
    assert DaySchedule("GPV3.2", date(2026, 10, 15)).outages == ()


def test_non_mapping_status_counts_as_not_ok():
    document = make_document({TODAY: {}})
    document["lastUpdateStatus"] = ["ok"]
    assert parse_schedule(document, GROUP, KYIV).source_ok is False


def test_out_of_range_day_key_is_an_error():
    document = make_document({TODAY: {}})
    document["fact"]["data"]["99999999999999999999"] = {GROUP: make_slots({})}
    with pytest.raises(ScheduleParseError):
        parse_schedule(document, GROUP, KYIV)


def test_naive_last_updated_is_read_as_utc():
    document = make_document({TODAY: {}})
    document["lastUpdated"] = "2026-10-15T20:31:23"
    schedule = parse_schedule(document, GROUP, KYIV)
    assert schedule.source_updated == datetime(2026, 10, 15, 20, 31, 23, tzinfo=UTC)


def test_fall_back_day_outage_spans_real_hours():
    day = parse_schedule(make_document({FALL_BACK: {3: "no", 4: "no", 5: "no"}}), GROUP, KYIV).days[FALL_BACK]
    assert day.outages == (Outage(local(2026, 10, 25, 2), local(2026, 10, 25, 5)),)
    assert day.outages[0].duration == timedelta(hours=4)
    assert DaySchedule.from_dict(day.to_dict()) == day


def test_fall_back_repeated_wall_hour_lasts_two_real_hours():
    outage = parse_schedule(make_document({FALL_BACK: {4: "no"}}), GROUP, KYIV).days[FALL_BACK].outages[0]
    assert outage.duration == timedelta(hours=2)


@pytest.mark.parametrize("state", ["no", "maybe", "second"])
def test_nonexistent_spring_forward_hour_yields_no_outage(state):
    day = parse_schedule(make_document({SPRING_FORWARD: {4: state}}), GROUP, KYIV).days[SPRING_FORWARD]
    assert day.outages == ()


@pytest.mark.parametrize("day", [FALL_BACK, SPRING_FORWARD])
@pytest.mark.parametrize("state", ["no", "first", "second", "maybe", "mfirst", "msecond", "weird"])
def test_every_outage_on_dst_days_has_positive_length(day, state):
    schedule = parse_schedule(make_document({day: {slot: state for slot in range(1, 25)}}), GROUP, KYIV)
    assert all(outage.start < outage.end for outage in schedule.days[day].outages)


@pytest.mark.parametrize("slot", ["0", "25"])
def test_out_of_range_slot_is_an_error(slot):
    document = make_document({TODAY: {}})
    next(iter(document["fact"]["data"].values()))[GROUP][slot] = "no"
    with pytest.raises(ScheduleParseError):
        parse_schedule(document, GROUP, KYIV)


def test_spring_forward_day_skips_the_missing_hour():
    day = parse_schedule(make_document({SPRING_FORWARD: {3: "no", 4: "no", 5: "no"}}), GROUP, KYIV).days[
        SPRING_FORWARD
    ]
    assert len(day.outages) == 1
    assert day.outages[0].duration == timedelta(hours=2)


@pytest.mark.parametrize("data", [{}, []])
def test_an_empty_document_plans_no_outages(data):
    document = {"lastUpdated": "2026-07-01T10:00:00Z", "lastUpdateStatus": {"ok": True}, "fact": {"data": data}}
    schedule = parse_schedule(document, GROUP, KYIV)
    assert schedule.days == {}
    assert schedule.day(TODAY) == DaySchedule(GROUP, TODAY)
    assert schedule.timeline() == ()


def test_an_empty_document_from_a_failed_update_is_an_error():
    document = {"lastUpdateStatus": {"ok": False}, "fact": {"data": {}}}
    with pytest.raises(ScheduleParseError, match="failed update"):
        parse_schedule(document, GROUP, KYIV)


def test_a_list_document_is_an_error():
    with pytest.raises(ScheduleParseError):
        parse_schedule([], GROUP, KYIV)
