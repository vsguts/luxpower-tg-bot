import logging
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lxbot.ecoflow.api import DEFAULT_API_URL, EcoFlowConfig
from lxbot.luxpower.client import DongleConfig
from lxbot.luxpower.frame import SERIAL_LEN
from lxbot.monitor.detector import Thresholds
from lxbot.monitor.forecast import BatteryConfig
from lxbot.nut.server import UpsConfig
from lxbot.schedule.parse import group_key
from lxbot.schedule.service import DEFAULT_URL, SETTLE_MAX, ScheduleConfig
from lxbot.tuya.api import DEFAULT_API_URL as TUYA_DEFAULT_API_URL
from lxbot.tuya.api import TuyaConfig

_REQUIRED = ("BOT_TOKEN", "ADMIN_CHAT_ID")
_DONGLE_REQUIRED = ("DONGLE_HOST", "DONGLE_SERIAL", "INVERTER_SERIAL")
_ECOFLOW_REQUIRED = ("ECOFLOW_ACCESS_KEY", "ECOFLOW_SECRET_KEY", "ECOFLOW_SERIAL")
_RETIRED_SCHEDULE_SETTINGS = ("DIGEST_TODAY_AT", "DIGEST_TOMORROW_AT", "SCHEDULE_PENDING_AT")

log = logging.getLogger(__name__)

T = TypeVar("T", int, float)
_QUEUE = re.compile(r"\d+\.\d+", re.ASCII)
_TUYA_ID = re.compile(r"[A-Za-z0-9]+", re.ASCII)
_MINUTES = re.compile(r"[0-9]+", re.ASCII)
_MAX_REPEAT_MINUTES = 24 * 60

LUXPOWER = "luxpower"
ECOFLOW = "ecoflow"
# EcoFlow does not publish its cloud rate limit: ask it half as often as the local dongle.
_POLL_INTERVALS = {LUXPOWER: "15", ECOFLOW: "30"}
_TRUE, _FALSE = frozenset({"1", "true", "yes"}), frozenset({"0", "false", "no"})

DeviceConfig = DongleConfig | EcoFlowConfig


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_chat_id: int
    device: DeviceConfig
    thresholds: Thresholds
    poll_interval: float
    tz: ZoneInfo
    db_path: Path
    schedule: ScheduleConfig | None = None
    tuya: TuyaConfig | None = None
    ups: UpsConfig | None = None
    has_generator: bool = True


def _require(env: Mapping[str, str], names: tuple[str, ...]) -> None:
    missing = [name for name in names if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")


def _number(env: Mapping[str, str], name: str, default: str, convert: Callable[[str], T]) -> T:
    try:
        # An empty value, as left in a copied .env.example, means the default.
        return convert(env.get(name, "").strip() or default)
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


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    if value in _TRUE | _FALSE:
        return value in _TRUE
    raise ConfigError(f"{name} must be true or false")


def _device(env: Mapping[str, str]) -> str:
    device = env.get("DEVICE", "").strip().lower() or LUXPOWER
    if device not in _POLL_INTERVALS:
        raise ConfigError(f"DEVICE must be {LUXPOWER} or {ECOFLOW}")
    return device


def _timezone(env: Mapping[str, str]) -> ZoneInfo:
    name = env.get("TZ", "Europe/Kyiv")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TZ is not a known time zone: {name}") from exc


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
    # The schedule now goes out whenever it appears or changes, at no fixed time. A stale setting must not
    # stop the bot (and its grid alerts) from starting, so it only warns.
    for name in _RETIRED_SCHEDULE_SETTINGS:
        if env.get(name):
            log.warning("%s is no longer used: the schedule is posted whenever it appears or changes", name)
    queue = queue.removeprefix("GPV")
    if not _QUEUE.fullmatch(queue):
        raise ConfigError("SCHEDULE_GROUP must look like 3.2")
    config = ScheduleConfig(
        group=group_key(queue),
        url=env.get("SCHEDULE_URL") or DEFAULT_URL,
        poll_interval=_duration(env, "SCHEDULE_POLL_INTERVAL", "300", "seconds"),
        match_window=_duration(env, "SCHEDULE_MATCH_WINDOW", "30", "minutes"),
        settle=_duration(env, "SCHEDULE_SETTLE_MINUTES", "20", "minutes"),
    )
    if config.settle > SETTLE_MAX:
        raise ConfigError(f"SCHEDULE_SETTLE_MINUTES must be at most {int(SETTLE_MAX.total_seconds() // 60)}")
    return config


def tuya_id(value: str, name: str) -> str:
    if not _TUYA_ID.fullmatch(value):
        raise ConfigError(f"{name} must be a Tuya ID (letters and digits)")
    return value


def _tuya_scene(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return tuya_id(value, name) if value else None


def tuya_keys_from_env(env: Mapping[str, str]) -> TuyaConfig | None:
    access_id, secret = env.get("TUYA_ACCESS_ID", "").strip(), env.get("TUYA_ACCESS_SECRET", "").strip()
    if not access_id and not secret:
        return None
    if not access_id or not secret:
        raise ConfigError("TUYA_ACCESS_ID and TUYA_ACCESS_SECRET must be set together")
    api_url = (env.get("TUYA_API_URL", "").strip() or TUYA_DEFAULT_API_URL).rstrip("/")
    # The access token travels in a header.
    if not api_url.startswith("https://"):
        raise ConfigError("TUYA_API_URL must be an https:// URL")
    return TuyaConfig(access_id=access_id, access_secret=secret, api_url=api_url)


def tuya_config_from_env(env: Mapping[str, str]) -> TuyaConfig | None:
    keys = tuya_keys_from_env(env)
    lost, restored = _tuya_scene(env, "TUYA_SCENE_GRID_LOST"), _tuya_scene(env, "TUYA_SCENE_GRID_RESTORED")
    if keys is None:
        if lost or restored:
            raise ConfigError("TUYA_SCENE_* needs TUYA_ACCESS_ID and TUYA_ACCESS_SECRET")
        return None
    # The keys alone are what tools.tuya_scenes needs to look the scenes up: not an error, just nothing to run.
    if not lost and not restored:
        log.warning("Tuya keys are set but no TUYA_SCENE_*: no scene will run")
        return None
    minutes = env.get("TUYA_SCENE_REPEAT_MINUTES", "").strip() or "10"
    if not _MINUTES.fullmatch(minutes) or int(minutes) > _MAX_REPEAT_MINUTES:
        raise ConfigError(f"TUYA_SCENE_REPEAT_MINUTES must be whole minutes from 0 (off) to {_MAX_REPEAT_MINUTES}")
    repeat = timedelta(minutes=int(minutes)) if int(minutes) else None
    return replace(keys, grid_lost_scene=lost, grid_restored_scene=restored, repeat=repeat)


def ups_config_from_env(env: Mapping[str, str], battery: BatteryConfig | None = None) -> UpsConfig | None:
    # Who may reach the port is the network's business; the server only reads and never takes commands.
    if env.get("NUT_ALLOWED_HOSTS", "").strip():
        log.warning("NUT_ALLOWED_HOSTS is no longer used: NUT_PORT alone turns the UPS server on")
    if not env.get("NUT_PORT", "").strip():
        return None
    port = _number(env, "NUT_PORT", "", int)
    if not 1 <= port <= 65535:
        raise ConfigError("NUT_PORT must be between 1 and 65535")
    # One above the inverter's cut-off (BATTERY_RESERVE_SOC): below it the inverter is already off.
    reserve = battery.reserve_soc if battery else 5
    low_soc = _number(env, "NUT_LOW_SOC", "", int) if env.get("NUT_LOW_SOC", "").strip() else reserve + 1
    if not reserve < low_soc <= 50:
        raise ConfigError(f"NUT_LOW_SOC must be above the battery reserve ({reserve}) and at most 50")
    return UpsConfig(
        port=port,
        low_soc=low_soc,
        user=env.get("NUT_USER", "").strip() or "monuser",
        password=env.get("NUT_PASSWORD", "").strip() or "secret",
    )


def dongle_config_from_env(env: Mapping[str, str]) -> DongleConfig:
    return DongleConfig(
        host=env["DONGLE_HOST"],
        port=_number(env, "DONGLE_PORT", "8000", int),
        dongle_serial=_serial(env, "DONGLE_SERIAL"),
        inverter_serial=_serial(env, "INVERTER_SERIAL"),
    )


def ecoflow_config_from_env(env: Mapping[str, str]) -> EcoFlowConfig:
    _require(env, _ECOFLOW_REQUIRED)
    return EcoFlowConfig(
        access_key=env["ECOFLOW_ACCESS_KEY"].strip(),
        secret_key=env["ECOFLOW_SECRET_KEY"].strip(),
        serial=env["ECOFLOW_SERIAL"].strip(),
        api_url=(env.get("ECOFLOW_API_URL") or DEFAULT_API_URL).rstrip("/"),
    )


def settings_from_env(env: Mapping[str, str] = os.environ) -> Settings:
    device = _device(env)
    _require(env, _REQUIRED + (_DONGLE_REQUIRED if device == LUXPOWER else _ECOFLOW_REQUIRED))
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
        device=dongle_config_from_env(env) if device == LUXPOWER else ecoflow_config_from_env(env),
        thresholds=thresholds,
        poll_interval=_positive(env, "POLL_INTERVAL", _POLL_INTERVALS[device]),
        tz=_timezone(env),
        db_path=Path(env.get("DB_PATH", "data/bot.sqlite3")),
        schedule=schedule_config_from_env(env),
        tuya=tuya_config_from_env(env),
        ups=ups_config_from_env(env, thresholds.battery),
        has_generator=_flag(env, "HAS_GENERATOR", default=device == LUXPOWER),
    )
