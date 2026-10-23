from aiohttp import test_utils, web

from lxbot.tuya.api import SCENE_TRIGGER_PATH, TOKEN_PATH, TuyaConfig, signature, string_to_sign

ACCESS_ID = "test-access-id"
SECRET = "test-secret"
SCENE = "sceneLost01"


class FakeTuya:
    """Local Tuya OpenAPI that checks request signatures and tokens like the real cloud."""

    def __init__(self):
        self.tokens_issued = 0
        self.expire_time = 7200
        self.reject_next_token = False
        self.code: int | None = None
        self.status = 200
        self.result: object = True
        self.triggered: list[str] = []
        self.content_types: list[str] = []
        self.url = ""
        self._valid_tokens: set[str] = set()
        self._server: test_utils.TestServer | None = None

    def config(self, secret: str = SECRET) -> TuyaConfig:
        return TuyaConfig(ACCESS_ID, secret, api_url=self.url, grid_lost_scene=SCENE, timeout=2)

    def forget_tokens(self) -> None:
        self._valid_tokens.clear()

    async def __aenter__(self) -> "FakeTuya":
        app = web.Application()
        app.router.add_get(TOKEN_PATH, self._token)
        app.router.add_post(SCENE_TRIGGER_PATH, self._trigger)
        self._server = test_utils.TestServer(app)
        await self._server.start_server()
        self.url = str(self._server.make_url("")).rstrip("/")
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._server.close()

    async def _token(self, request: web.Request) -> web.Response:
        self.content_types.append(request.headers.get("Content-Type", ""))
        if not await self._signed(request, token=""):
            return _failure(1004, "sign invalid")
        self.tokens_issued += 1
        token = f"token-{self.tokens_issued}"
        self._valid_tokens.add(token)
        return _success({"access_token": token, "expire_time": self.expire_time, "uid": "owner"})

    def issue_bad_expiry(self) -> None:
        self.expire_time = "soon"

    async def _trigger(self, request: web.Request) -> web.Response:
        if self.status != 200:
            return web.Response(status=self.status)
        token = request.headers.get("access_token", "")
        if token not in self._valid_tokens:
            return _failure(1010, "token invalid")
        if not await self._signed(request, token):
            return _failure(1004, "sign invalid")
        if self.code is not None:
            return _failure(self.code, "permission deny")
        self.content_types.append(request.headers.get("Content-Type", ""))
        self.triggered.append(request.match_info["rule_id"])
        return _success(self.result)

    @staticmethod
    async def _signed(request: web.Request, token: str) -> bool:
        headers = request.headers
        to_sign = string_to_sign(request.method, request.path, dict(request.query), await request.read())
        expected = signature(ACCESS_ID, SECRET, headers["t"], headers["nonce"], to_sign, token)
        return headers["client_id"] == ACCESS_ID and headers["sign_method"] == "HMAC-SHA256" and headers["sign"] == expected


def _success(result: object) -> web.Response:
    return web.json_response({"success": True, "result": result, "t": 0})


def _failure(code: int, msg: str) -> web.Response:
    return web.json_response({"success": False, "code": code, "msg": msg, "t": 0})
