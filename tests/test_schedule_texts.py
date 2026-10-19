from dataclasses import replace
from datetime import UTC, date, timedelta

import pytest

from lxbot.bot import texts
from lxbot.monitor.detector import Audience, Debounced, Event, EventType, State
from lxbot.schedule.compare import MatchKind, ScheduleMatch
from lxbot.schedule.model import DaySchedule, Outage, Schedule, ScheduleUpdate
from tests.factories import make_snapshot
from tests.schedule_factories import GROUP, KYIV, TODAY, TOMORROW, local

MORNING = Outage(local(2026, 10, 15, 11), local(2026, 10, 15, 15))
EVENING = Outage(local(2026, 10, 15, 20, 30), local(2026, 10, 16, 0))
POSSIBLE = Outage(local(2026, 10, 15, 18), local(2026, 10, 15, 19), certain=False)
TODAY_SCHEDULE = DaySchedule(GROUP, TODAY, (MORNING, POSSIBLE, EVENING))
SCHEDULE = Schedule(GROUP, {TODAY: TODAY_SCHEDULE})


def test_range_ending_at_midnight_reads_24_00():
    assert texts.format_range(MORNING, KYIV) == "з 11:00 до 15:00"
    assert texts.format_range(EVENING, KYIV) == "з 20:30 до 24:00"
    night = Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 1))
    assert texts.format_range(night, KYIV) == "з 23:00 до 01:00"


def test_utc_stored_outages_render_in_local_time():
    evening = Outage(EVENING.start.astimezone(UTC), EVENING.end.astimezone(UTC))
    morning = Outage(MORNING.start.astimezone(UTC), MORNING.end.astimezone(UTC))
    assert texts.format_range(evening, KYIV) == "з 20:30 до 24:00"
    assert texts.format_range(morning, KYIV) == "з 11:00 до 15:00"
    match = ScheduleMatch(MatchKind.RESTORED, morning, timedelta(minutes=-12), evening)
    assert texts.schedule_lines(match, local(2026, 10, 15, 14, 48).astimezone(UTC), KYIV) == [
        "⏱ За графіком до 15:00 — на 12 хв раніше",
        "📅 Наступне відключення: сьогодні з 20:30 до 24:00",
    ]
    lost = ScheduleMatch(MatchKind.LOST, morning, timedelta(minutes=18))
    assert texts.schedule_lines(lost, local(2026, 10, 15, 11, 18).astimezone(UTC), KYIV) == [
        "⏱ За графіком з 11:00 — на 18 хв пізніше",
        "⏳ Очікуємо світло о 15:00",
    ]


@pytest.mark.parametrize(
    ("deviation", "expected"),
    [
        (timedelta(minutes=18), "на 18 хв пізніше"),
        (timedelta(minutes=-12), "на 12 хв раніше"),
        (timedelta(minutes=2), "вчасно"),
        (timedelta(minutes=-1), "вчасно"),
        (timedelta(hours=1, minutes=5), "на 1 год 5 хв пізніше"),
    ],
)
def test_format_deviation(deviation, expected):
    assert texts.format_deviation(deviation) == expected


def test_lost_on_schedule_lines():
    match = ScheduleMatch(MatchKind.LOST, MORNING, timedelta(minutes=18))
    assert texts.schedule_lines(match, local(2026, 10, 15, 11, 18), KYIV) == [
        "⏱ За графіком з 11:00 — на 18 хв пізніше",
        "⏳ Очікуємо світло о 15:00",
    ]


def test_lost_on_a_possible_outage():
    match = ScheduleMatch(MatchKind.LOST, POSSIBLE, timedelta())
    assert texts.schedule_lines(match, local(2026, 10, 15, 18), KYIV)[0] == "⏱ За можливим відключенням з 18:00 — вчасно"


def test_lost_off_schedule():
    assert texts.schedule_lines(ScheduleMatch(MatchKind.LOST), local(2026, 10, 15, 9), KYIV) == ["⚠️ Не за графіком"]


def test_restored_lines_name_the_next_outage():
    match = ScheduleMatch(MatchKind.RESTORED, MORNING, timedelta(minutes=-12), EVENING)
    assert texts.schedule_lines(match, local(2026, 10, 15, 14, 48), KYIV) == [
        "⏱ За графіком до 15:00 — на 12 хв раніше",
        "📅 Наступне відключення: сьогодні з 20:30 до 24:00",
    ]


def test_restored_off_schedule_with_nothing_planned():
    match = ScheduleMatch(MatchKind.RESTORED)
    assert texts.schedule_lines(match, local(2026, 10, 15, 9), KYIV) == [
        "⚠️ Не за графіком",
        "📅 Відключень за графіком більше не заплановано",
    ]


def test_next_outage_tomorrow():
    tomorrow = Outage(local(2026, 10, 16, 11), local(2026, 10, 16, 15))
    match = ScheduleMatch(MatchKind.RESTORED, EVENING, timedelta(), tomorrow)
    lines = texts.schedule_lines(match, local(2026, 10, 16, 0), KYIV)
    assert lines[-1] == "📅 Наступне відключення: сьогодні з 11:00 до 15:00"
    lines = texts.schedule_lines(match, local(2026, 10, 15, 23, 59), KYIV)
    assert lines[-1] == "📅 Наступне відключення: завтра з 11:00 до 15:00"


def test_grid_lost_message_gets_schedule_lines():
    event = Event(
        EventType.GRID_LOST,
        local(2026, 10, 15, 11, 18),
        87,
        previous_duration=timedelta(hours=16, minutes=52),
        match=ScheduleMatch(MatchKind.LOST, MORNING, timedelta(minutes=18)),
    )
    text = texts.event_text(event, KYIV)
    assert text.splitlines() == [
        "🔴 <b>11:18 Світло зникло</b>",
        "🕓 Воно було 16 год 52 хв",
        "🔋 Заряд 87%",
        "⏱ За графіком з 11:00 — на 18 хв пізніше",
        "⏳ Очікуємо світло о 15:00",
    ]


def test_grid_restored_message_keeps_generator_line_before_schedule():
    event = Event(
        EventType.GRID_RESTORED,
        local(2026, 10, 15, 14, 48),
        60,
        previous_duration=timedelta(hours=3),
        gen_was_running=True,
        match=ScheduleMatch(MatchKind.RESTORED, MORNING, timedelta(minutes=-12), None),
    )
    lines = texts.event_text(event, KYIV).splitlines()
    assert lines[2:4] == ["🔋 Заряд 60%", "⛽ Генератор можна вимикати"]
    assert lines[4:] == ["⏱ За графіком до 15:00 — на 12 хв раніше", "📅 Відключень за графіком більше не заплановано"]


TOMORROW_SCHEDULE = DaySchedule(GROUP, TOMORROW, (Outage(local(2026, 10, 16, 11), local(2026, 10, 16, 15)),))


def update_text(at, *days, published=(), changed=(), source_updated=None):
    update = ScheduleUpdate(tuple(days), tuple(published), tuple(changed))
    return texts.event_text(Event(EventType.SCHEDULE_UPDATE, at, update=update, source_updated=source_updated), KYIV)


def test_a_new_tomorrow_goes_out_with_today():
    text = update_text(local(2026, 10, 15, 9), TODAY_SCHEDULE, TOMORROW_SCHEDULE, published=(TOMORROW,))
    assert text.splitlines() == [
        "📅 <b>Опубліковано графік на завтра</b> · черга 3.2",
        "",
        "<b>Сьогодні</b>, четвер, 15 жовтня",
        "🕐 з 11:00 до 15:00 (~4 год)",
        "❔ можливо з 18:00 до 19:00",
        "🕐 з 20:30 до 24:00 (~3.5 год)",
        "Без світла: 7 год 30 хв",
        "",
        "<b>Завтра</b>, пʼятниця, 16 жовтня",
        "🕐 з 11:00 до 15:00 (~4 год)",
        "Без світла: 4 год",
    ]


def test_a_day_without_outages():
    text = update_text(local(2026, 10, 15, 21), DaySchedule(GROUP, TOMORROW), published=(TOMORROW,))
    assert text.splitlines()[2:] == ["<b>Завтра</b>, пʼятниця, 16 жовтня", "✅ Відключень не заплановано"]


@pytest.mark.parametrize(
    ("published", "changed", "headline"),
    [
        ((TODAY, TOMORROW), (), "📅 <b>Опубліковано графік на сьогодні й завтра</b> · черга 3.2"),
        ((), (TODAY,), "🔄 <b>Змінився графік на сьогодні</b> · черга 3.2"),
        ((), (TODAY, TOMORROW), "🔄 <b>Змінився графік на сьогодні й завтра</b> · черга 3.2"),
        ((TOMORROW,), (TODAY,), "🔄 <b>Змінився графік на сьогодні, опубліковано на завтра</b> · черга 3.2"),
    ],
)
def test_the_headline_says_what_happened(published, changed, headline):
    text = update_text(
        local(2026, 10, 15, 9), TODAY_SCHEDULE, TOMORROW_SCHEDULE, published=published, changed=changed
    )
    assert text.splitlines()[0] == headline


def test_a_day_after_tomorrow_is_named_by_its_date():
    day = DaySchedule(GROUP, date(2026, 10, 17))
    text = update_text(local(2026, 10, 15, 9), day, published=(day.day,))
    assert text.splitlines()[0] == "📅 <b>Опубліковано графік на 17.10</b> · черга 3.2"
    assert text.splitlines()[2] == "<b>Субота, 17 жовтня</b>"


def test_an_update_from_old_data_warns_about_its_age():
    updated = local(2026, 10, 15, 7, 5).astimezone(UTC)
    at = local(2026, 10, 15, 21)
    lines = update_text(at, TODAY_SCHEDULE, changed=(TODAY,), source_updated=updated).splitlines()
    assert lines[-2:] == ["", "⚠️ Дані могли застаріти: джерело оновлювалось 15.10 07:05"]
    assert lines[:-2] == update_text(at, TODAY_SCHEDULE, changed=(TODAY,)).splitlines()


def test_payload_includes_the_update_and_source_updated_only_when_set():
    update = ScheduleUpdate((TODAY_SCHEDULE, TOMORROW_SCHEDULE), published=(TOMORROW,))
    event = Event(EventType.SCHEDULE_UPDATE, local(2026, 10, 15, 9), update=update)
    payload = event.to_payload()
    assert "source_updated" not in payload
    assert payload["update"] == {
        "days": [TODAY_SCHEDULE.to_dict(), TOMORROW_SCHEDULE.to_dict()],
        "published": ["2026-10-16"],
        "changed": [],
    }
    stale = replace(event, source_updated=local(2026, 10, 15, 7, 5))
    assert stale.to_payload()["source_updated"] == local(2026, 10, 15, 7, 5).isoformat()


def test_source_health_texts_go_to_the_admin():
    down = Event(EventType.SCHEDULE_SOURCE_DOWN, local(2026, 10, 15, 21, 5))
    assert texts.event_text(down, KYIV) == "⚠️ Графік відключень не оновлюється з 21:05"
    assert down.audience == Audience.ADMIN
    up = Event(EventType.SCHEDULE_SOURCE_UP, local(2026, 10, 15, 22))
    assert texts.event_text(up, KYIV) == "✅ Графік відключень знову оновлюється"
    assert up.audience == Audience.ADMIN
    assert Event(EventType.SCHEDULE_UPDATE, local(2026, 10, 15, 9)).audience == Audience.SUBSCRIBERS


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (12, 0, "📅 За графіком світла немає до 15:00"),
        (18, 30, "📅 Можливе відключення до 19:00"),
        (16, 0, "📅 Можливе відключення: сьогодні з 18:00 до 19:00"),
        (19, 30, "📅 Наступне відключення: сьогодні з 20:30 до 24:00"),
        (23, 59, "📅 За графіком світла немає до 24:00"),
    ],
)
def test_schedule_status_line(hour, minute, expected):
    assert texts.schedule_status_line(SCHEDULE, local(2026, 10, 15, hour, minute), KYIV) == expected


def test_schedule_status_line_with_nothing_planned():
    schedule = Schedule(GROUP, {TODAY: DaySchedule(GROUP, TODAY)})
    assert texts.schedule_status_line(schedule, local(2026, 10, 15, 12), KYIV) == "📅 Відключень за графіком не заплановано"


def test_status_text_includes_the_schedule_line():
    snapshot = make_snapshot(0)
    state = State(grid=Debounced(True, snapshot.taken_at), gen=Debounced(False, snapshot.taken_at))
    now = local(2026, 10, 15, 12)
    with_schedule = texts.status_text(snapshot, state, now, KYIV, SCHEDULE).splitlines()
    assert "📅 За графіком світла немає до 15:00" in with_schedule
    assert with_schedule.index("📅 За графіком світла немає до 15:00") == with_schedule.index("☀️ Сонце: 0 Вт") + 1
    assert not any(line.startswith("📅") for line in texts.status_text(snapshot, state, now, KYIV).splitlines())
    assert texts.status_text(None, None, now, KYIV, SCHEDULE) == f"{texts.NO_DATA}\n📅 За графіком світла немає до 15:00"


def test_payload_includes_the_match_only_when_set():
    plain = Event(EventType.GRID_LOST, local(2026, 10, 15, 11), 80, timedelta(hours=1))
    assert set(plain.to_payload()) == {"soc", "previous_duration_s", "gen_was_running"}
    matched = replace(plain, match=ScheduleMatch(MatchKind.LOST))
    assert matched.to_payload()["match"] == ScheduleMatch(MatchKind.LOST).to_payload()


def test_today_strikes_out_what_has_passed():
    night = Outage(local(2026, 10, 17, 0), local(2026, 10, 17, 2))
    morning = Outage(local(2026, 10, 17, 8, 30), local(2026, 10, 17, 12))
    evening = Outage(local(2026, 10, 17, 18, 30), local(2026, 10, 17, 22, 30))
    day = DaySchedule(GROUP, date(2026, 10, 17), (night, morning, evening))
    assert update_text(local(2026, 10, 17, 9), day, changed=(day.day,)).splitlines()[3:] == [
        "🕐 <s>з 00:00 до 02:00 (~2 год)</s>",
        "🕐 з 08:30 до 12:00 (~3.5 год)",
        "🕐 з 18:30 до 22:30 (~4 год)",
        "Без світла: 9 год 30 хв · ще попереду 7 год",
    ]


def test_today_after_every_outage_says_so():
    day = DaySchedule(GROUP, TODAY, (MORNING,))
    assert update_text(local(2026, 10, 15, 16), day, changed=(TODAY,)).splitlines()[3:] == [
        "🕐 <s>з 11:00 до 15:00 (~4 год)</s>",
        "Без світла: 4 год · усі вже минули",
    ]


def test_tomorrow_has_nothing_struck_out():
    text = update_text(local(2026, 10, 14, 21), TODAY_SCHEDULE, published=(TODAY,))
    assert "<s>" not in text
    assert text.splitlines()[-1] == "Без світла: 7 год 30 хв"


def test_still_ahead_ignores_the_seconds_of_now():
    night = Outage(local(2026, 10, 17, 0), local(2026, 10, 17, 2))
    evening = Outage(local(2026, 10, 17, 18, 30), local(2026, 10, 17, 22, 30))
    day = DaySchedule(GROUP, date(2026, 10, 17), (night, evening))
    now = local(2026, 10, 17, 1).replace(second=17)
    assert update_text(now, day, changed=(day.day,)).splitlines()[-1] == "Без світла: 6 год · ще попереду 5 год"
