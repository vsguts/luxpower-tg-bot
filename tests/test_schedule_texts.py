from dataclasses import replace
from datetime import UTC, timedelta

import pytest

from lxbot.bot import texts
from lxbot.monitor.detector import Audience, Debounced, Event, EventType, State
from lxbot.schedule.compare import MatchKind, ScheduleMatch
from lxbot.schedule.model import DaySchedule, Outage, Schedule
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
    assert lines[2] == "⛽ Генератор можна вимикати"
    assert lines[3:] == ["⏱ За графіком до 15:00 — на 12 хв раніше", "📅 Відключень за графіком більше не заплановано"]


def test_today_digest():
    text = texts.event_text(Event(EventType.SCHEDULE_TODAY, local(2026, 10, 15, 9), schedule=TODAY_SCHEDULE), KYIV)
    assert text.splitlines() == [
        "📅 <b>Графік на сьогодні</b> · четвер, 15 жовтня · черга 3.2",
        "🕐 з 11:00 до 15:00 (~4 год)",
        "❔ можливо з 18:00 до 19:00",
        "🕐 з 20:30 до 24:00 (~3.5 год)",
        "Без світла: 7 год 30 хв",
    ]


def test_tomorrow_digest_without_outages():
    schedule = DaySchedule(GROUP, TOMORROW)
    text = texts.event_text(Event(EventType.SCHEDULE_TOMORROW, local(2026, 10, 15, 21), schedule=schedule), KYIV)
    assert text.splitlines() == [
        "📅 <b>Графік на завтра</b> · пʼятниця, 16 жовтня · черга 3.2",
        "✅ Відключень не заплановано",
    ]


def test_tomorrow_pending_and_published():
    pending = texts.event_text(Event(EventType.SCHEDULE_TOMORROW_PENDING, local(2026, 10, 15, 21)), KYIV)
    assert pending == "⏳ Графік на завтра ще не опубліковано"
    schedule = DaySchedule(GROUP, TOMORROW, (Outage(local(2026, 10, 16, 11), local(2026, 10, 16, 15)),))
    published = texts.event_text(
        Event(EventType.SCHEDULE_TOMORROW_PUBLISHED, local(2026, 10, 15, 22), schedule=schedule), KYIV
    )
    assert published.splitlines()[0] == "📅 <b>Графік на завтра опубліковано</b> · пʼятниця, 16 жовтня · черга 3.2"



@pytest.mark.parametrize(
    "event_type",
    [
        EventType.SCHEDULE_TODAY,
        EventType.SCHEDULE_TOMORROW,
        EventType.SCHEDULE_TOMORROW_PUBLISHED,
        EventType.SCHEDULE_CHANGED,
    ],
)
def test_digest_from_an_unhealthy_source_warns_about_its_age(event_type):
    updated = local(2026, 10, 15, 7, 5).astimezone(UTC)
    event = Event(event_type, local(2026, 10, 15, 21), schedule=TODAY_SCHEDULE, source_updated=updated)
    lines = texts.event_text(event, KYIV).splitlines()
    assert lines[-1] == "⚠️ Дані могли застаріти: джерело оновлювалось 15.10 07:05"
    assert lines[:-1] == texts.event_text(replace(event, source_updated=None), KYIV).splitlines()


def test_payload_includes_source_updated_only_when_set():
    digest = Event(EventType.SCHEDULE_TODAY, local(2026, 10, 15, 9), schedule=TODAY_SCHEDULE)
    assert "source_updated" not in digest.to_payload()
    stale = replace(digest, source_updated=local(2026, 10, 15, 7, 5))
    assert stale.to_payload()["source_updated"] == local(2026, 10, 15, 7, 5).isoformat()

@pytest.mark.parametrize(("day", "word"), [(TODAY, "сьогодні"), (TOMORROW, "завтра")])
def test_changed_notice(day, word):
    event = Event(EventType.SCHEDULE_CHANGED, local(2026, 10, 15, 22), schedule=DaySchedule(GROUP, day))
    assert texts.event_text(event, KYIV).splitlines()[0].startswith(f"🔄 <b>Графік на {word} змінився</b>")


def test_source_health_texts_go_to_the_admin():
    down = Event(EventType.SCHEDULE_SOURCE_DOWN, local(2026, 10, 15, 21, 5))
    assert texts.event_text(down, KYIV) == "⚠️ Графік відключень не оновлюється з 21:05"
    assert down.audience == Audience.ADMIN
    up = Event(EventType.SCHEDULE_SOURCE_UP, local(2026, 10, 15, 22))
    assert texts.event_text(up, KYIV) == "✅ Графік відключень знову оновлюється"
    assert up.audience == Audience.ADMIN
    assert Event(EventType.SCHEDULE_TODAY, local(2026, 10, 15, 9)).audience == Audience.SUBSCRIBERS


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


def test_payload_includes_schedule_and_match_only_when_set():
    plain = Event(EventType.GRID_LOST, local(2026, 10, 15, 11), 80, timedelta(hours=1))
    assert set(plain.to_payload()) == {"soc", "previous_duration_s", "gen_was_running"}
    rich = Event(
        EventType.SCHEDULE_TODAY,
        local(2026, 10, 15, 9),
        schedule=TODAY_SCHEDULE,
        match=ScheduleMatch(MatchKind.LOST),
    )
    payload = rich.to_payload()
    assert payload["schedule"] == TODAY_SCHEDULE.to_dict()
    assert payload["match"] == ScheduleMatch(MatchKind.LOST).to_payload()
