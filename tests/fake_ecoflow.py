from aiohttp import test_utils, web

from lxbot.ecoflow.api import DEVICE_LIST_PATH, QUOTA_ALL_PATH, EcoFlowConfig, signature

ACCESS_KEY = "test-access"
SECRET_KEY = "test-secret"
SERIAL = "DCABZ0000000001"


class FakeCloud:
    """Local EcoFlow IoT API that checks request signatures like the real cloud."""

    def __init__(self):
        self.devices: object = [{"sn": SERIAL, "online": 1, "productName": "DELTA Pro"}]
        self.quota: object = {"bmsMaster.soc": 87, "inv.acInVol": 0}
        self.code = "0"
        self.status = 200
        self.url = ""
        self._server: test_utils.TestServer | None = None

    def config(self, secret_key: str = SECRET_KEY) -> EcoFlowConfig:
        return EcoFlowConfig(ACCESS_KEY, secret_key, SERIAL, api_url=self.url, timeout=2)

    async def __aenter__(self) -> "FakeCloud":
        app = web.Application()
        app.router.add_get(DEVICE_LIST_PATH, self._device_list)
        app.router.add_get(QUOTA_ALL_PATH, self._quota_all)
        self._server = test_utils.TestServer(app)
        await self._server.start_server()
        self.url = str(self._server.make_url("")).rstrip("/")
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._server.close()

    async def _device_list(self, request: web.Request) -> web.Response:
        return await self._reply(request, self.devices)

    async def _quota_all(self, request: web.Request) -> web.Response:
        return await self._reply(request, self.quota if request.query.get("sn") == SERIAL else None)

    async def _reply(self, request: web.Request, data: object) -> web.Response:
        if self.status != 200:
            return web.Response(status=self.status)
        if not self._verified(request):
            return web.json_response({"code": "8521", "message": "signature is wrong"})
        if self.code != "0":
            return web.json_response({"code": self.code, "message": "boom"})
        return web.json_response({"code": "0", "message": "Success", "data": data})

    @staticmethod
    def _verified(request: web.Request) -> bool:
        headers = request.headers
        expected = signature(dict(request.query), ACCESS_KEY, SECRET_KEY, headers["nonce"], headers["timestamp"])
        return headers["accessKey"] == ACCESS_KEY and headers["sign"] == expected
