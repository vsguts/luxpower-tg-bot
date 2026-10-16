import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lxbot.luxpower.client import DongleConfig
from lxbot.luxpower.frame import SERIAL_LEN
from lxbot.monitor.detector import Thresholds
from lxbot.monitor.forecast import BatteryConfig
from lxbot.schedule.parse import group_key
from lxbot.schedule.service import DEFAULT_URL, ScheduleConfig

_REQUIRED = ("BOT_TOKEN", "ADMIN_CHAT_ID", "DONGLE_HOST", "DONGLE_SERIAL", "INVERTER_SERIAL")

T = TypeVar("T", int, float)
_QUEUE = re.compile(r"\d+\.\d+", re.ASCII)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_chat_id: int
    dongle: DongleConfig
    thresholds: Thresholds
    poll_interval: float
    tz: ZoneInfo
    db_path: Path
    schedule: ScheduleConfig | None = None


def _number(env: Mapping[str, str], name: str, default: str, convert: Callable[[str], T]) -> T:
    try:
        return convert(env.get(name, default))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _positive(env: Mapping[str, str], name: str, default: str) -> float:
    value = _number(env, name, default, float)
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return value


def _duration(env: Mapping[str, str], name: str, default: str, unit: str) -> timedelta:
    try:
        return timedelta(**{unit: _positive(env, name, default)})
    except OverflowError as exc:
        raise ConfigError(f"{name} is too large") from exc


def _serial(env: Mapping[str, str], name: str) -> str:
    value = env[name]
    if len(value) != SERIAL_LEN or not value.isascii():
        raise ConfigError(f"{name} must be {SERIAL_LEN} ASCII characters")
    return value


def _timezone(env: Mapping[str, str]) -> ZoneInfo:
    name = env.get("TZ", "Europe/Kyiv")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TZ is not a known time zone: {name}") from exc


def _clock_time(env: Mapping[str, str], name: str, default: str) -> time:
    value = env.get(name, default)
    try:
        moment = time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be HH:MM") from exc
    # Digest times are compared with naive local clock times: an offset would fail every tick.
    if moment.tzinfo is not None:
        raise ConfigError(f"{name} must be HH:MM without a time zone")
    return moment


def battery_config_from_env(env: Mapping[str, str]) -> BatteryConfig | None:
    if not env.get("BATTERY_CAPACITY_WH", "").strip():
        return None
    reserve = _number(env, "BATTERY_RESERVE_SOC", "5", int)
    if not 0 <= reserve < 100:
        raise ConfigError("BATTERY_RESERVE_SOC must be between 0 and 99")
    return BatteryConfig(capacity_wh=_positive(env, "BATTERY_CAPACITY_WH", ""), reserve_soc=reserve)


def schedule_config_from_env(env: Mapping[str, str]) -> ScheduleConfig | None:
    queue = env.get("SCHEDULE_GROUP", "").strip()
    if not queue:
        return None
    queue = queue.removeprefix("GPV")
    if not _QUEUE.fullmatch(queue):
        raise ConfigError("SCHEDULE_GROUP must look like 3.2")
    config = ScheduleConfig(
        group=group_key(queue),
        url=env.get("SCHEDULE_URL") or DEFAULT_URL,
        poll_interval=_duration(env, "SCHEDULE_POLL_INTERVAL", "900", "seconds"),
        match_window=_duration(env, "SCHEDULE_MATCH_WINDOW", "30", "minutes"),
        today_at=_clock_time(env, "DIGEST_TODAY_AT", "09:00"),
        tomorrow_at=_clock_time(env, "DIGEST_TOMORROW_AT", "21:00"),
    )
    if not config.today_at < config.tomorrow_at:
        raise ConfigError("DIGEST_TODAY_AT must be earlier than DIGEST_TOMORROW_AT")
    return config


def dongle_config_from_env(env: Mapping[str, str]) -> DongleConfig:
    return DongleConfig(
        host=env["DONGLE_HOST"],
        port=_number(env, "DONGLE_PORT", "8000", int),
        dongle_serial=_serial(env, "DONGLE_SERIAL"),
        inverter_serial=_serial(env, "INVERTER_SERIAL"),
    )


def settings_from_env(env: Mapping[str, str] = os.environ) -> Settings:
    missing = [name for name in _REQUIRED if not env.get(name)]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    thresholds = Thresholds(
        soc_low=_number(env, "SOC_LOW", "30", int),
        soc_critical=_number(env, "SOC_CRITICAL", "15", int),
        soc_gen_full=_number(env, "SOC_GEN_FULL", "90", int),
        battery=battery_config_from_env(env),
    )
    if not thresholds.soc_critical < thresholds.soc_low < thresholds.soc_gen_full:
        raise ConfigError("thresholds must satisfy SOC_CRITICAL < SOC_LOW < SOC_GEN_FULL")
    return Settings(
        bot_token=env["BOT_TOKEN"],
        admin_chat_id=_number(env, "ADMIN_CHAT_ID", "", int),
        dongle=dongle_config_from_env(env),
        thresholds=thresholds,
        poll_interval=_positive(env, "POLL_INTERVAL", "15"),
        tz=_timezone(env),
        db_path=Path(env.get("DB_PATH", "data/bot.sqlite3")),
        schedule=schedule_config_from_env(env),
    )
