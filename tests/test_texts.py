from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from lxbot.bot import texts
from lxbot.monitor.detector import Debounced, Event, EventType, State
from lxbot.storage.db import Subscriber, SubscriberStatus
from tests.factories import at, make_snapshot

KYIV = ZoneInfo("Europe/Kyiv")


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (timedelta(seconds=30), "менше хвилини"),
        (timedelta(minutes=45), "45 хв"),
        (timedelta(hours=3), "3 год"),
        (timedelta(hours=16, minutes=52), "16 год 52 хв"),
        (timedelta(days=2, hours=3, minutes=5), "2 дн 3 год"),
    ],
)
def test_format_duration(duration, expected):
    assert texts.format_duration(duration) == expected


@pytest.mark.parametrize(("watts", "expected"), [(850, "850 Вт"), (1200, "1.2 кВт"), (-1200, "1.2 кВт")])
def test_format_power(watts, expected):
    assert texts.format_power(watts) == expected


def test_grid_lost_text_uses_kyiv_time():
    event = Event(EventType.GRID_LOST, at(72), 87, previous_duration=timedelta(hours=16, minutes=52))
    text = texts.event_text(event, KYIV)
    assert "10:18 Світло зникло" in text  # 07:18 UTC is 10:18 EEST
    assert "16 год 52 хв" in text
    assert "87%" in text


def test_grid_restored_mentions_generator_only_when_it_ran():
    with_gen = Event(EventType.GRID_RESTORED, at(0), 60, timedelta(hours=1), gen_was_running=True)
    without = Event(EventType.GRID_RESTORED, at(0), 60, timedelta(hours=1))
    assert "Генератор можна вимикати" in texts.event_text(with_gen, KYIV)
    assert "Генератор" not in texts.event_text(without, KYIV)


@pytest.mark.parametrize(
    ("event_type", "fragment"),
    [
        (EventType.BATTERY_LOW, "Час заводити генератор"),
        (EventType.BATTERY_CRITICAL, "Терміново заведіть генератор"),
        (EventType.GEN_CHARGED, "Генератор можна вимикати"),
        (EventType.DEVICE_DOWN, "Немає зв'язку з інвертором"),
        (EventType.DEVICE_UP, "Зв'язок з інвертором відновлено"),
    ],
)
def test_other_event_texts(event_type, fragment):
    assert fragment in texts.event_text(Event(event_type, at(0), 30), KYIV)


def test_journal_only_events_have_no_text():
    assert texts.event_text(Event(EventType.GEN_STARTED, at(0)), KYIV) is None


def test_status_text_on_grid():
    snapshot = make_snapshot(0, soc=76, battery_w=1200, load_w=850, pv_w=0, grid_v=229.4)
    state = State(grid=Debounced(True, at(0)), gen=Debounced(False, at(0)))
    text = texts.status_text(snapshot, state, at(1), KYIV)
    assert "⚡ Мережа: є · 229 В" in text
    assert "⛽ Генератор: не працює" in text
    assert "🔋 Батарея: 76% · заряджається 1.2 кВт" in text
    assert "🏠 Споживання: 850 Вт" in text
    assert "Оновлено 10:00:00" in text
    assert "застарілі" not in text


def test_status_text_off_grid_shows_outage_length():
    snapshot = make_snapshot(540, grid=False, battery_w=-500)
    state = State(grid=Debounced(False, at(0)), gen=Debounced(False, at(0)))
    text = texts.status_text(snapshot, state, at(540), KYIV)
    assert "немає вже 2 год 15 хв" in text
    assert "розряджається 500 Вт" in text


def test_status_text_marks_stale_data():
    state = State(grid=Debounced(True, at(0)), gen=Debounced(False, at(0)))
    text = texts.status_text(make_snapshot(0), state, at(0) + timedelta(minutes=3), KYIV)
    assert "застарілі" in text


def test_status_text_without_data():
    assert texts.status_text(None, None, at(0), KYIV) == texts.NO_DATA


def test_admin_request_text_escapes_html():
    text = texts.admin_request_text("Оля <3 & Co", "olya", 42)
    assert "Оля &lt;3 &amp; Co" in text
    assert "@olya" in text
    assert "42" in text


def test_removed_text_keeps_name_unescaped_for_plain_text_toast():
    assert texts.removed_text("Оля <3 & Co") == "Видалено: Оля <3 & Co"


def test_subscribers_text_marks_each_status():
    def subscriber(chat_id, name, status):
        return Subscriber(chat_id, name, None, status, at(0), at(0))

    text = texts.subscribers_text(
        [
            subscriber(42, "Оля", SubscriberStatus.APPROVED),
            subscriber(43, "Петро", SubscriberStatus.PENDING),
            subscriber(44, "Ігор", SubscriberStatus.REJECTED),
        ]
    )
    assert text.splitlines()[1:] == ["✅ Оля", "⏳ Петро", "🚫 Ігор"]


def test_status_text_says_when_a_grid_change_is_being_checked():
    present = State(grid=Debounced(True, at(0)), gen=Debounced(False, at(0)))
    absent = State(grid=Debounced(False, at(0)), gen=Debounced(False, at(0)))
    going = texts.status_text(make_snapshot(1, grid=False), present, at(1), KYIV)
    coming = texts.status_text(make_snapshot(1), absent, at(1), KYIV)
    assert "⚡ Мережа: є · 0 В (перевіряю)" in going
    assert "⚡ Мережа: немає вже менше хвилини (перевіряю)" in coming


def test_status_text_does_not_check_a_sagging_grid_the_detector_holds():
    state = State(grid=Debounced(True, at(0)), gen=Debounced(False, at(0)))
    text = texts.status_text(make_snapshot(1, grid_v=175.0), state, at(1), KYIV)
    assert "⚡ Мережа: є · 175 В\n" in text
