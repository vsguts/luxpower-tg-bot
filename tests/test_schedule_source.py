import json

import pytest
from aiohttp import test_utils, web

from lxbot.schedule.source import HttpScheduleSource, ScheduleSourceError


class Remote:
    def __init__(self):
        self.status = 200
        self.body = json.dumps({"fact": {"data": {}}})
        self.etag: str | None = '"v1"'
        self.seen_etags: list[str | None] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.seen_etags.append(request.headers.get("If-None-Match"))
        if self.etag and request.headers.get("If-None-Match") == self.etag:
            return web.Response(status=304)
        headers = {"ETag": self.etag} if self.etag else {}
        return web.Response(status=self.status, text=self.body, content_type="text/plain", headers=headers)


@pytest.fixture
async def remote():
    state = Remote()
    app = web.Application()
    app.router.add_get("/kyiv-region.json", state.handle)
    # Via the module: a bare `TestServer` name in a test module is collected by pytest as a test class.
    server = test_utils.TestServer(app)
    await server.start_server()
    state.url = str(server.make_url("/kyiv-region.json"))
    yield state
    await server.close()


@pytest.fixture
async def source(remote):
    source = HttpScheduleSource(remote.url, timeout=2)
    yield source
    await source.close()


async def test_fetch_returns_the_document(source):
    assert await source.fetch() == {"fact": {"data": {}}}


async def test_unchanged_document_returns_none(remote, source):
    await source.fetch()
    assert await source.fetch() is None
    assert remote.seen_etags == [None, '"v1"']


async def test_without_etag_every_fetch_returns_the_document(remote, source):
    remote.etag = None
    assert await source.fetch() is not None
    assert await source.fetch() is not None


async def test_http_error(remote, source):
    remote.status = 500
    with pytest.raises(ScheduleSourceError, match="HTTP 500"):
        await source.fetch()


async def test_invalid_json(remote, source):
    remote.body = "<html>oops</html>"
    with pytest.raises(ScheduleSourceError):
        await source.fetch()


async def test_json_that_is_not_an_object(remote, source):
    remote.body = "[1, 2]"
    with pytest.raises(ScheduleSourceError, match="not a JSON object"):
        await source.fetch()


async def test_unreachable_source():
    source = HttpScheduleSource("http://127.0.0.1:9/kyiv-region.json", timeout=2)
    with pytest.raises(ScheduleSourceError):
        await source.fetch()
    await source.close()
