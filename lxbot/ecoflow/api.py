"""Signed requests to the EcoFlow IoT Developer API (developer-eu.ecoflow.com)."""

import hashlib
import hmac
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus

import aiohttp

from lxbot.device.snapshot import DeviceError

DEFAULT_API_URL = "https://api-e.ecoflow.com"
DEVICE_LIST_PATH = "/iot-open/sign/device/list"
QUOTA_ALL_PATH = "/iot-open/sign/device/quota/all"
SUCCESS_CODE = "0"


class EcoFlowError(DeviceError):
    pass


@dataclass(frozen=True)
class EcoFlowConfig:
    access_key: str
    secret_key: str = field(repr=False)
    serial: str
    api_url: str = DEFAULT_API_URL
    timeout: float = 10.0


def _flatten(params: Mapping[str, object], prefix: str = "") -> Iterator[tuple[str, str]]:
    for key, value in params.items():
        if isinstance(value, Mapping):
            yield from _flatten(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", str(value)


def signature(params: Mapping[str, object], access_key: str, secret_key: str, nonce: str, timestamp: str) -> str:
    pairs = sorted(_flatten(params)) + [("accessKey", access_key), ("nonce", nonce), ("timestamp", timestamp)]
    message = "&".join(f"{key}={value}" for key, value in pairs)
    return hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()


class EcoFlowApi:
    def __init__(self, config: EcoFlowConfig):
        self._config = config
        self._timeout = aiohttp.ClientTimeout(total=config.timeout)
        self._session: aiohttp.ClientSession | None = None

    async def devices(self) -> list[dict]:
        data = await self._get(DEVICE_LIST_PATH, {})
        if not isinstance(data, list):
            raise EcoFlowError("the device list is not a JSON array")
        return data

    async def quota(self) -> dict[str, object]:
        data = await self._get(QUOTA_ALL_PATH, {"sn": self._config.serial})
        if not isinstance(data, dict):
            raise EcoFlowError("the quota is not a JSON object")
        return data

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _headers(self, params: Mapping[str, str]) -> dict[str, str]:
        nonce = f"{secrets.randbelow(1_000_000):06d}"
        timestamp = str(time.time_ns() // 1_000_000)
        config = self._config
        return {
            "accessKey": config.access_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": signature(params, config.access_key, config.secret_key, nonce, timestamp),
        }

    async def _get(self, path: str, params: Mapping[str, str]) -> object:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        try:
            async with self._session.get(
                self._config.api_url + path, params=params, headers=self._headers(params)
            ) as response:
                if response.status != HTTPStatus.OK:
                    raise EcoFlowError(f"HTTP {response.status} from {path}")
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise EcoFlowError(f"requesting {path}: {exc!r}") from exc
        if not isinstance(body, dict):
            raise EcoFlowError(f"the reply from {path} is not a JSON object")
        if str(body.get("code")) != SUCCESS_CODE:
            raise EcoFlowError(f"{path} failed with code {body.get('code')}: {body.get('message')}")
        return body.get("data")
