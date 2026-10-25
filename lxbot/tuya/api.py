"""Signed requests to the Tuya cloud OpenAPI (openapi.tuyaeu.com for the Central Europe data center)."""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus

import aiohttp

DEFAULT_API_URL = "https://openapi.tuyaeu.com"
TOKEN_PATH = "/v1.0/token"
SCENE_TRIGGER_PATH = "/v2.0/cloud/scene/rule/{rule_id}/actions/trigger"
# "token invalid" and "token expired": the cloud dropped the token before its expire_time.
TOKEN_REJECTED_CODES = frozenset({1010, 1011})
# A token used in its last minute may expire on the way to the cloud.
TOKEN_EXPIRY_MARGIN = 60.0


class TuyaError(Exception):
    pass


class _TokenRejected(TuyaError):
    pass


@dataclass(frozen=True)
class TuyaConfig:
    access_id: str
    access_secret: str = field(repr=False)
    api_url: str = DEFAULT_API_URL
    grid_lost_scene: str | None = None
    grid_restored_scene: str | None = None
    # How often the grid-lost scene runs again while the grid is out; None runs it once.
    repeat: timedelta | None = timedelta(minutes=10)
    timeout: float = 10.0


def string_to_sign(
    method: str,
    path: str,
    query: Mapping[str, str] | None = None,
    body: bytes = b"",
    signed_headers: Mapping[str, str] | None = None,
) -> str:
    url = path
    if query:
        url += "?" + "&".join(f"{key}={value}" for key, value in sorted(query.items()))
    headers = "".join(f"{key}:{value}\n" for key, value in (signed_headers or {}).items())
    return "\n".join([method, hashlib.sha256(body).hexdigest(), headers, url])


def signature(access_id: str, secret: str, t: str, nonce: str, to_sign: str, access_token: str = "") -> str:
    message = access_id + access_token + t + nonce + to_sign
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


class TuyaApi:
    def __init__(self, config: TuyaConfig, *, clock: Callable[[], float] = time.time):
        self._config = config
        self._clock = clock
        self._timeout = aiohttp.ClientTimeout(total=config.timeout)
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        self._closed = False

    async def trigger_scene(self, rule_id: str) -> None:
        result = await self.request("POST", SCENE_TRIGGER_PATH.format(rule_id=rule_id))
        if result is not True:
            raise TuyaError(f"scene {rule_id} was not triggered: {result!r}")

    async def request(
        self, method: str, path: str, query: Mapping[str, str] | None = None, body: object = None
    ) -> object:
        token = await self._access_token()
        try:
            return await self._call(method, path, query, body, token)
        except _TokenRejected:
            # Another call may already have replaced it.
            if self._token == token:
                self._token = None
            return await self._call(method, path, query, body, await self._access_token())

    async def close(self) -> None:
        self._closed = True
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token is None or self._clock() >= self._token_expires - TOKEN_EXPIRY_MARGIN:
                result = await self._call("GET", TOKEN_PATH, {"grant_type": "1"}, None, token="")
                if not isinstance(result, dict) or not result.get("access_token"):
                    raise TuyaError("the token reply has no access_token")
                try:
                    lifetime = float(result.get("expire_time", 0))
                except (TypeError, ValueError) as exc:
                    raise TuyaError(f"the token reply has a bad expire_time: {result.get('expire_time')!r}") from exc
                self._token, self._token_expires = str(result["access_token"]), self._clock() + lifetime
            return self._token

    def _headers(self, method: str, path: str, query: Mapping[str, str] | None, body: bytes, token: str) -> dict:
        config = self._config
        t, nonce = str(int(self._clock() * 1000)), uuid.uuid4().hex
        to_sign = string_to_sign(method, path, query, body)
        # Tuya's own SDKs send it on every call; without it aiohttp labels an empty POST octet-stream.
        headers = {
            "Content-Type": "application/json",
            "client_id": config.access_id,
            "t": t,
            "nonce": nonce,
            "sign_method": "HMAC-SHA256",
            "sign": signature(config.access_id, config.access_secret, t, nonce, to_sign, token),
        }
        if token:
            headers["access_token"] = token
        return headers

    async def _call(
        self, method: str, path: str, query: Mapping[str, str] | None, body: object, token: str
    ) -> object:
        if self._closed:
            raise TuyaError("the Tuya client is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        payload = json.dumps(body).encode() if body is not None else b""
        try:
            async with self._session.request(
                method,
                self._config.api_url + path,
                params=query,
                data=payload or None,
                headers=self._headers(method, path, query, payload, token),
            ) as response:
                if response.status != HTTPStatus.OK:
                    raise TuyaError(f"HTTP {response.status} from {path}")
                reply = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            # repr() of aiohttp's errors carries the request headers, the access token among them.
            raise TuyaError(f"requesting {path}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(reply, dict):
            raise TuyaError(f"the reply from {path} is not a JSON object")
        if not reply.get("success"):
            error = _TokenRejected if token and reply.get("code") in TOKEN_REJECTED_CODES else TuyaError
            raise error(f"{path} failed with code {reply.get('code')}: {reply.get('msg')}")
        return reply.get("result")
