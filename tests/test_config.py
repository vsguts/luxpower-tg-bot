from pathlib import Path

import pytest

from lxbot.config import ConfigError, settings_from_env
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
