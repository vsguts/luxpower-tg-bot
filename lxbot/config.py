import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lxbot.luxpower.client import DongleConfig
from lxbot.luxpower.frame import SERIAL_LEN
from lxbot.monitor.detector import Thresholds

_REQUIRED = ("BOT_TOKEN", "ADMIN_CHAT_ID", "DONGLE_HOST", "DONGLE_SERIAL", "INVERTER_SERIAL")

T = TypeVar("T", int, float)


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


def _number(env: Mapping[str, str], name: str, default: str, convert: Callable[[str], T]) -> T:
    try:
        return convert(env.get(name, default))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


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
    )
    if not thresholds.soc_critical < thresholds.soc_low < thresholds.soc_gen_full:
        raise ConfigError("thresholds must satisfy SOC_CRITICAL < SOC_LOW < SOC_GEN_FULL")
    return Settings(
        bot_token=env["BOT_TOKEN"],
        admin_chat_id=_number(env, "ADMIN_CHAT_ID", "", int),
        dongle=dongle_config_from_env(env),
        thresholds=thresholds,
        poll_interval=_number(env, "POLL_INTERVAL", "15", float),
        tz=_timezone(env),
        db_path=Path(env.get("DB_PATH", "data/bot.sqlite3")),
    )
