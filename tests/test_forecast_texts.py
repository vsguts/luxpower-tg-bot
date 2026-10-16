from datetime import timedelta
from zoneinfo import ZoneInfo

from lxbot.bot import texts
from lxbot.monitor.detector import Debounced, Event, EventType, State
from lxbot.monitor.forecast import BatteryConfig, Forecast
from tests.factories import at, make_snapshot

KYIV = ZoneInfo("Europe/Kyiv")
BATTERY = BatteryConfig(capacity_wh=16000, reserve_soc=5)
FULL = Forecast(1000, timedelta(hours=3, minutes=30), timedelta(hours=2, minutes=40), 55, timedelta(hours=2))
POWER_ONLY = Forecast(1000, timedelta(hours=12, minutes=50))


def lines(event):
    return texts.event_text(event, KYIV).splitlines()


def test_restored_message_shows_the_charge():
    event = Event(EventType.GRID_RESTORED, at(0), 76, previous_duration=timedelta(hours=3))
    assert lines(event) == ["🟢 <b>10:00 Світло з'явилось</b>", "🕓 Його не було 3 год", "🔋 Заряд 76%"]


def test_restored_message_keeps_the_generator_line_after_the_charge():
    event = Event(EventType.GRID_RESTORED, at(0), 76, previous_duration=timedelta(hours=3), gen_was_running=True)
    assert lines(event)[2:] == ["🔋 Заряд 76%", "⛽ Генератор можна вимикати"]


def test_lost_message_forecasts_by_power():
    event = Event(EventType.GRID_LOST, at(0), 85, previous_duration=timedelta(hours=5), forecast=POWER_ONLY)
    assert lines(event)[2:] == ["🔋 Заряд 85%", "⏳ Вистачить: ~12 год 50 хв при 1.0 кВт"]


def test_lost_message_without_a_forecast_is_unchanged():
    event = Event(EventType.GRID_LOST, at(0), 85, previous_duration=timedelta(hours=5))
    assert lines(event) == ["🔴 <b>10:00 Світло зникло</b>", "🕓 Воно було 5 год", "🔋 Заряд 85%"]


def test_battery_alerts_show_both_forecasts():
    for event_type, first in [
        (EventType.BATTERY_LOW, "🪫 <b>Залишилось 30%</b>"),
        (EventType.BATTERY_CRITICAL, "🚨 <b>Залишилось 30%</b>"),
    ]:
        result = lines(Event(event_type, at(0), 30, forecast=FULL))
        assert result[0] == first
        assert result[2:] == [
            "⏳ Вистачить: ~3 год 30 хв при 1.0 кВт",
            "📉 За темпом розряду: ~2 год 40 хв (−55% за 2 год)",
        ]


def test_an_almost_empty_battery_reads_less_than_ten_minutes():
    event = Event(EventType.BATTERY_CRITICAL, at(0), 5, forecast=Forecast(900, timedelta()))
    assert lines(event)[2] == "⏳ Вистачить: менше 10 хв при 900 Вт"


def _off_grid_state(drain_soc, drain_since):
    return State(
        grid=Debounced(False, at(0)),
        gen=Debounced(False, at(0)),
        drain_soc=drain_soc,
        drain_since=drain_since,
    )


def test_status_on_battery_shows_both_forecasts():
    snapshot = make_snapshot(480, grid=False, soc=60, battery_w=-1100)
    status = texts.status_text(snapshot, _off_grid_state(72, at(0)), at(480), KYIV, battery=BATTERY).splitlines()
    battery_line = status.index("🔋 Батарея: 60% · розряджається 1.1 кВт")
    assert status[battery_line + 1 : battery_line + 3] == [
        "⏳ Вистачить: ~8 год при 1.1 кВт",
        "📉 За темпом розряду: ~9 год 10 хв (−12% за 2 год)",
    ]


def test_status_says_when_the_trend_needs_more_data():
    snapshot = make_snapshot(10, grid=False, soc=72, battery_w=-1100)
    status = texts.status_text(snapshot, _off_grid_state(72, at(0)), at(10), KYIV, battery=BATTERY)
    assert "📉 За темпом розряду: ще мало даних" in status


def test_status_without_battery_config_or_on_grid_has_no_forecast():
    off_grid = make_snapshot(480, grid=False, soc=60, battery_w=-1100)
    status = texts.status_text(off_grid, _off_grid_state(72, at(0)), at(480), KYIV)
    assert "Вистачить" not in status
    on_grid = make_snapshot(0, soc=60, battery_w=-200)
    state = State(grid=Debounced(True, at(0)), gen=Debounced(False, at(0)))
    assert "Вистачить" not in texts.status_text(on_grid, state, at(0), KYIV, battery=BATTERY)


def test_unsupported_text_hints_the_commands():
    assert texts.unsupported_text(admin=False) == (
        "🤖 Я не розумію текстових повідомлень.\nДоступно: 📊 Статус (/status), /stop"
    )
    assert texts.unsupported_text(admin=True).endswith("/stop, /subscribers")
