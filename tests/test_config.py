from datetime import timedelta
from pathlib import Path

import pytest

from lxbot.config import ConfigError, ecoflow_config_from_env, settings_from_env
from lxbot.monitor.forecast import BatteryConfig
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
    assert schedule.poll_interval == timedelta(minutes=5)
    assert schedule.match_window == timedelta(minutes=30)
    assert schedule.settle == timedelta(minutes=20)


def test_schedule_overrides():
    env = ENV | {
        "SCHEDULE_GROUP": "6.2",
        "SCHEDULE_URL": "https://example.test/x.json",
        "SCHEDULE_POLL_INTERVAL": "600",
        "SCHEDULE_MATCH_WINDOW": "20",
        "SCHEDULE_SETTLE_MINUTES": "15",
    }
    schedule = settings_from_env(env).schedule
    assert schedule.group == "GPV6.2"
    assert schedule.url == "https://example.test/x.json"
    assert schedule.poll_interval == timedelta(minutes=10)
    assert schedule.match_window == timedelta(minutes=20)
    assert schedule.settle == timedelta(minutes=15)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SCHEDULE_SETTLE_MINUTES", "soon", "SCHEDULE_SETTLE_MINUTES"),
        ("SCHEDULE_SETTLE_MINUTES", "0", "SCHEDULE_SETTLE_MINUTES"),
        ("SCHEDULE_SETTLE_MINUTES", "60", "SCHEDULE_SETTLE_MINUTES must be at most 45"),
        ("SCHEDULE_MATCH_WINDOW", "half", "SCHEDULE_MATCH_WINDOW"),
        ("SCHEDULE_POLL_INTERVAL", "often", "SCHEDULE_POLL_INTERVAL"),
    ],
)
def test_bad_schedule_values(name, value, message):
    with pytest.raises(ConfigError, match=message):
        settings_from_env(ENV | {"SCHEDULE_GROUP": "3.2", name: value})


@pytest.mark.parametrize("name", ["SCHEDULE_POLL_INTERVAL", "SCHEDULE_MATCH_WINDOW", "POLL_INTERVAL"])
@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "-inf", "1e400"])
def test_intervals_must_be_finite_and_positive(name, value):
    with pytest.raises(ConfigError, match=name):
        settings_from_env(ENV | {"SCHEDULE_GROUP": "3.2", name: value})


@pytest.mark.parametrize("name", ["SCHEDULE_POLL_INTERVAL", "SCHEDULE_MATCH_WINDOW"])
def test_schedule_durations_beyond_timedelta_are_named(name):
    with pytest.raises(ConfigError, match=name):
        settings_from_env(ENV | {"SCHEDULE_GROUP": "3.2", name: "1e300"})


def test_schedule_group_accepts_the_gpv_prefix():
    assert settings_from_env(ENV | {"SCHEDULE_GROUP": "GPV3.2"}).schedule.group == "GPV3.2"
    assert settings_from_env(ENV | {"SCHEDULE_GROUP": " 6.1 "}).schedule.group == "GPV6.1"


@pytest.mark.parametrize("value", ["3", "abc", "GPV", "3.2.1", "3.2a", "GPVGPV3.2", "gpv3.2", "3,2"])
def test_schedule_group_must_look_like_a_queue(value):
    with pytest.raises(ConfigError, match="SCHEDULE_GROUP must look like 3.2"):
        settings_from_env(ENV | {"SCHEDULE_GROUP": value})


def test_battery_forecast_is_off_without_a_capacity():
    assert settings_from_env(ENV).thresholds.battery is None


def test_battery_defaults_and_overrides():
    assert settings_from_env(ENV | {"BATTERY_CAPACITY_WH": "16000"}).thresholds.battery == BatteryConfig(16000, 5)
    env = ENV | {"BATTERY_CAPACITY_WH": "10240", "BATTERY_RESERVE_SOC": "10"}
    assert settings_from_env(env).thresholds.battery == BatteryConfig(10240, 10)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BATTERY_CAPACITY_WH", "-5"),
        ("BATTERY_CAPACITY_WH", "lots"),
        ("BATTERY_CAPACITY_WH", "nan"),
        ("BATTERY_RESERVE_SOC", "100"),
        ("BATTERY_RESERVE_SOC", "-1"),
        ("BATTERY_RESERVE_SOC", "five"),
    ],
)
def test_bad_battery_values_are_named(name, value):
    with pytest.raises(ConfigError, match=name):
        settings_from_env(ENV | {"BATTERY_CAPACITY_WH": "16000", name: value})


def test_ecoflow_config_defaults_to_the_european_cloud():
    config = ecoflow_config_from_env({
        "ECOFLOW_ACCESS_KEY": " access ",
        "ECOFLOW_SECRET_KEY": "secret",
        "ECOFLOW_SERIAL": "DCABZ0000000001",
    })
    assert (config.access_key, config.secret_key, config.serial) == ("access", "secret", "DCABZ0000000001")
    assert config.api_url == "https://api-e.ecoflow.com"


def test_ecoflow_api_url_override_drops_the_trailing_slash():
    env = {"ECOFLOW_ACCESS_KEY": "a", "ECOFLOW_SECRET_KEY": "s", "ECOFLOW_SERIAL": "x"}
    assert ecoflow_config_from_env(env | {"ECOFLOW_API_URL": "https://api-a.ecoflow.com/"}).api_url == (
        "https://api-a.ecoflow.com"
    )


def test_ecoflow_blank_credentials_are_missing():
    with pytest.raises(ConfigError, match="ECOFLOW_SECRET_KEY"):
        ecoflow_config_from_env({"ECOFLOW_ACCESS_KEY": "a", "ECOFLOW_SECRET_KEY": " ", "ECOFLOW_SERIAL": "x"})


@pytest.mark.parametrize("name", ["DIGEST_TODAY_AT", "DIGEST_TOMORROW_AT", "SCHEDULE_PENDING_AT"])
def test_retired_digest_times_only_warn(caplog, name):
    settings = settings_from_env(ENV | {"SCHEDULE_GROUP": "3.2", name: "21:00"})
    assert settings.schedule.settle == timedelta(minutes=20)
    assert name in caplog.text


TUYA_KEYS = {"TUYA_ACCESS_ID": "id123", "TUYA_ACCESS_SECRET": "secret"}


def test_tuya_is_off_without_keys():
    assert settings_from_env(ENV).tuya is None


def test_tuya_defaults():
    tuya = settings_from_env(ENV | TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "abcDEF123"}).tuya
    assert (tuya.access_id, tuya.access_secret) == ("id123", "secret")
    assert tuya.api_url == "https://openapi.tuyaeu.com"
    assert (tuya.grid_lost_scene, tuya.grid_restored_scene) == ("abcDEF123", None)


def test_tuya_overrides():
    env = ENV | TUYA_KEYS | {
        "TUYA_API_URL": "https://openapi-weaz.tuyaeu.com/",
        "TUYA_SCENE_GRID_LOST": "lost1",
        "TUYA_SCENE_GRID_RESTORED": "back1",
    }
    tuya = settings_from_env(env).tuya
    assert tuya.api_url == "https://openapi-weaz.tuyaeu.com"
    assert (tuya.grid_lost_scene, tuya.grid_restored_scene) == ("lost1", "back1")


def test_tuya_api_url_is_trimmed():
    env = ENV | TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "lost1", "TUYA_API_URL": " https://openapi.tuyaeu.com/ "}
    assert settings_from_env(env).tuya.api_url == "https://openapi.tuyaeu.com"


def test_tuya_keys_without_a_scene_only_warn(caplog):
    assert settings_from_env(ENV | TUYA_KEYS).tuya is None
    assert "TUYA_SCENE" in caplog.text


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"TUYA_ACCESS_ID": "id123", "TUYA_SCENE_GRID_LOST": "lost1"}, "must be set together"),
        ({"TUYA_SCENE_GRID_LOST": "lost1"}, "needs TUYA_ACCESS_ID"),
        (TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "lost/../x"}, "TUYA_SCENE_GRID_LOST must be a Tuya ID"),
        (TUYA_KEYS | {"TUYA_SCENE_GRID_RESTORED": "a b"}, "TUYA_SCENE_GRID_RESTORED must be a Tuya ID"),
        (TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "lost1", "TUYA_API_URL": "http://openapi.tuyaeu.com"}, "https://"),
        (TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "lost1", "TUYA_API_URL": "openapi.tuyaeu.com"}, "https://"),
    ],
)
def test_bad_tuya_settings(extra, message):
    with pytest.raises(ConfigError, match=message):
        settings_from_env(ENV | extra)


def test_tuya_scene_repeats_every_ten_minutes_by_default():
    tuya = settings_from_env(ENV | TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "lost1"}).tuya
    assert tuya.repeat == timedelta(minutes=10)


def test_tuya_scene_repeat_can_be_changed_or_turned_off():
    env = ENV | TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "lost1"}
    assert settings_from_env(env | {"TUYA_SCENE_REPEAT_MINUTES": "15"}).tuya.repeat == timedelta(minutes=15)
    assert settings_from_env(env | {"TUYA_SCENE_REPEAT_MINUTES": "0"}).tuya.repeat is None


def test_an_empty_tuya_scene_repeat_means_the_default():
    env = ENV | TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "lost1", "TUYA_SCENE_REPEAT_MINUTES": " "}
    assert settings_from_env(env).tuya.repeat == timedelta(minutes=10)


@pytest.mark.parametrize("value", ["-1", "often", "1.5", "1e3", "1_000", "٣", "1441", "99999999999999"])
def test_bad_tuya_scene_repeat(value):
    env = ENV | TUYA_KEYS | {"TUYA_SCENE_GRID_LOST": "lost1", "TUYA_SCENE_REPEAT_MINUTES": value}
    with pytest.raises(ConfigError, match="TUYA_SCENE_REPEAT_MINUTES"):
        settings_from_env(env)


def test_the_nut_ups_is_off_without_allowed_hosts():
    assert settings_from_env(ENV).ups is None


def test_nut_ups_defaults():
    ups = settings_from_env(ENV | {"NUT_ALLOWED_HOSTS": " 192.168.110.110 , 192.168.120.110"}).ups
    assert ups.allowed_hosts == frozenset({"192.168.110.110", "192.168.120.110"})
    assert (ups.low_soc, ups.port, ups.user, ups.password) == (6, 3493, "monuser", "secret")


def test_nut_ups_overrides():
    env = ENV | {
        "NUT_ALLOWED_HOSTS": "192.168.110.110",
        "NUT_LOW_SOC": "10",
        "NUT_PORT": "13493",
        "NUT_USER": "upsmon",
        "NUT_PASSWORD": "pw",
    }
    ups = settings_from_env(env).ups
    assert (ups.low_soc, ups.port, ups.user, ups.password) == (10, 13493, "upsmon", "pw")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"NUT_ALLOWED_HOSTS": "nas.lan"}, "NUT_ALLOWED_HOSTS"),
        ({"NUT_ALLOWED_HOSTS": "192.168.110.110", "NUT_LOW_SOC": "5"}, "above the battery reserve"),
        ({"NUT_ALLOWED_HOSTS": "192.168.110.110", "NUT_LOW_SOC": "60"}, "NUT_LOW_SOC"),
        ({"NUT_ALLOWED_HOSTS": "192.168.110.110", "NUT_LOW_SOC": "low"}, "NUT_LOW_SOC"),
        ({"NUT_ALLOWED_HOSTS": "192.168.110.110", "NUT_PORT": "70000"}, "NUT_PORT"),
    ],
)
def test_bad_nut_settings(extra, message):
    with pytest.raises(ConfigError, match=message):
        settings_from_env(ENV | extra)


def test_the_nut_threshold_follows_the_battery_reserve():
    env = ENV | {"NUT_ALLOWED_HOSTS": "192.168.110.110", "BATTERY_CAPACITY_WH": "16000", "BATTERY_RESERVE_SOC": "10"}
    assert settings_from_env(env).ups.low_soc == 11
    with pytest.raises(ConfigError, match="above the battery reserve"):
        settings_from_env(env | {"NUT_LOW_SOC": "10"})
