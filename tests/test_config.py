from datetime import time, timedelta
from pathlib import Path

import pytest

from lxbot.config import ConfigError, settings_from_env
from lxbot.schedule.service import DEFAULT_URL
from tests.fixtures import DONGLE_SERIAL, INVERTER_SERIAL

ENV = {
    "BOT_TOKEN": "token",
    "ADMIN_CHAT_ID": "12345",
    "DONGLE_HOST": "inverter.lan",
    "DONGLE_SERIAL": DONGLE_SERIAL,
    "INVERTER_SERIAL": INVERTER_SERIAL,
}


def test_defaults():
    settings = settings_from_env(ENV)
    assert settings.admin_chat_id == 12345
    assert settings.dongle.host == "inverter.lan"
    assert settings.dongle.port == 8000
    assert settings.poll_interval == 15
    assert (settings.thresholds.soc_low, settings.thresholds.soc_critical, settings.thresholds.soc_gen_full) == (30, 15, 90)
    assert settings.tz.key == "Europe/Kyiv"
    assert settings.db_path == Path("data/bot.sqlite3")


def test_overrides():
    env = ENV | {"DONGLE_PORT": "9000", "POLL_INTERVAL": "10", "SOC_LOW": "35", "TZ": "UTC", "DB_PATH": "/app/data/x.db"}
    settings = settings_from_env(env)
    assert settings.dongle.port == 9000
    assert settings.poll_interval == 10
    assert settings.thresholds.soc_low == 35
    assert settings.tz.key == "UTC"
    assert settings.db_path == Path("/app/data/x.db")


def test_missing_variables_are_listed():
    with pytest.raises(ConfigError, match="BOT_TOKEN, DONGLE_SERIAL"):
        settings_from_env({k: v for k, v in ENV.items() if k not in {"BOT_TOKEN", "DONGLE_SERIAL"}})


def test_bad_serial():
    with pytest.raises(ConfigError, match="DONGLE_SERIAL"):
        settings_from_env(ENV | {"DONGLE_SERIAL": "DG1"})


def test_thresholds_must_be_ordered():
    with pytest.raises(ConfigError, match="SOC_CRITICAL < SOC_LOW < SOC_GEN_FULL"):
        settings_from_env(ENV | {"SOC_CRITICAL": "40"})


def test_non_numeric_admin_chat_id_is_named():
    with pytest.raises(ConfigError, match="ADMIN_CHAT_ID"):
        settings_from_env(ENV | {"ADMIN_CHAT_ID": "abc"})


def test_non_numeric_poll_interval_is_named():
    with pytest.raises(ConfigError, match="POLL_INTERVAL"):
        settings_from_env(ENV | {"POLL_INTERVAL": "fast"})


def test_unknown_time_zone_is_named():
    with pytest.raises(ConfigError, match="TZ"):
        settings_from_env(ENV | {"TZ": "Mars/Base"})


def test_schedule_is_off_without_a_group():
    assert settings_from_env(ENV).schedule is None
    assert settings_from_env(ENV | {"SCHEDULE_GROUP": "  "}).schedule is None


def test_schedule_defaults():
    schedule = settings_from_env(ENV | {"SCHEDULE_GROUP": "3.2"}).schedule
    assert schedule.group == "GPV3.2"
    assert schedule.url == DEFAULT_URL
    assert schedule.poll_interval == timedelta(minutes=15)
    assert schedule.match_window == timedelta(minutes=30)
    assert (schedule.today_at, schedule.tomorrow_at) == (time(9), time(21))


def test_schedule_overrides():
    env = ENV | {
        "SCHEDULE_GROUP": "6.2",
        "SCHEDULE_URL": "https://example.test/x.json",
        "SCHEDULE_POLL_INTERVAL": "600",
        "SCHEDULE_MATCH_WINDOW": "20",
        "DIGEST_TODAY_AT": "08:30",
        "DIGEST_TOMORROW_AT": "20:00",
    }
    schedule = settings_from_env(env).schedule
    assert schedule.group == "GPV6.2"
    assert schedule.url == "https://example.test/x.json"
    assert schedule.poll_interval == timedelta(minutes=10)
    assert schedule.match_window == timedelta(minutes=20)
    assert (schedule.today_at, schedule.tomorrow_at) == (time(8, 30), time(20))


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DIGEST_TODAY_AT", "9am", "DIGEST_TODAY_AT"),
        ("DIGEST_TOMORROW_AT", "25:00", "DIGEST_TOMORROW_AT"),
        ("SCHEDULE_MATCH_WINDOW", "half", "SCHEDULE_MATCH_WINDOW"),
        ("SCHEDULE_POLL_INTERVAL", "often", "SCHEDULE_POLL_INTERVAL"),
        ("DIGEST_TODAY_AT", "22:00", "earlier than DIGEST_TOMORROW_AT"),
    ],
)
def test_bad_schedule_values(name, value, message):
    with pytest.raises(ConfigError, match=message):
        settings_from_env(ENV | {"SCHEDULE_GROUP": "3.2", name: value})
