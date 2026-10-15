import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from lxbot.schedule.model import DaySchedule, Outage
from lxbot.schedule.parse import ScheduleParseError, group_key, parse_schedule
from tests.schedule_factories import GROUP, KYIV, TODAY, TOMORROW, local, make_document

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "kyiv-region-sample.json").read_text())


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
