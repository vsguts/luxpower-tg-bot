from http import HTTPStatus
from typing import Protocol

import aiohttp


class ScheduleSourceError(Exception):
    pass


class ScheduleSource(Protocol):
    async def fetch(self) -> dict | None: ...

    async def close(self) -> None: ...


class HttpScheduleSource:
    def __init__(self, url: str, timeout: float = 20.0):
        self._url = url
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._etag: str | None = None

    async def fetch(self) -> dict | None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        headers = {"If-None-Match": self._etag} if self._etag else {}
        try:
            async with self._session.get(self._url, headers=headers) as response:
                if response.status == HTTPStatus.NOT_MODIFIED:
                    return None
                if response.status != HTTPStatus.OK:
                    raise ScheduleSourceError(f"HTTP {response.status} from {self._url}")
                # raw.githubusercontent.com serves JSON as text/plain.
                document = await response.json(content_type=None)
                etag = response.headers.get("ETag")
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ScheduleSourceError(f"fetching {self._url}: {exc!r}") from exc
        if not isinstance(document, dict):
            raise ScheduleSourceError("the schedule document is not a JSON object")
        self._etag = etag
        return document

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
