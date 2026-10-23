import asyncio

import pytest

from lxbot.tuya.api import TuyaApi, TuyaConfig, TuyaError, signature, string_to_sign
from tests.fake_tuya import ACCESS_ID, SCENE, SECRET, FakeTuya

# The worked examples from Tuya's "Sign requests for cloud authorization" page.
DOC_ID, DOC_SECRET = "1KAD46OrT9HafiKdsXeg", "4OHBOnWOqaEC1mWXOpVL3yV50s0qGSRC"
DOC_T, DOC_NONCE = "1588925778000", "5138cc3a9033d69856923fd07b491173"
DOC_HEADERS = {"area_id": "29a33e8796834b1efa6", "call_id": "8afdb70ab2ed11eb85290242ac130003"}


def test_token_request_signature_matches_the_documented_example():
    to_sign = string_to_sign("GET", "/v1.0/token", {"grant_type": "1"}, signed_headers=DOC_HEADERS)
    sign = signature(DOC_ID, DOC_SECRET, DOC_T, DOC_NONCE, to_sign)
    assert sign == "9E48A3E93B302EEECC803C7241985D0A34EB944F40FB573C7B5C2A82158AF13E"


def test_business_request_signature_matches_the_documented_example():
    query = {"page_size": "50", "page_no": "1"}
    to_sign = string_to_sign("GET", "/v2.0/apps/schema/users", query, signed_headers=DOC_HEADERS)
    sign = signature(DOC_ID, DOC_SECRET, DOC_T, DOC_NONCE, to_sign, "3f4eda2bdec17232f67c0b188af3eec1")
    assert sign == "AE4481C692AA80B25F3A7E12C3A5FD9BBF6251539DD78E565A1A72A508A88784"


def test_string_to_sign_without_query_or_body():
    assert string_to_sign("POST", "/v2.0/x") == (
        "POST\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n\n/v2.0/x"
    )


def test_config_defaults_to_central_europe_and_hides_the_secret():
    config = TuyaConfig(ACCESS_ID, SECRET)
    assert config.api_url == "https://openapi.tuyaeu.com"
    assert SECRET not in repr(config)


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
async def cloud():
    async with FakeTuya() as fake:
        yield fake


@pytest.fixture
async def clock():
    return Clock()


@pytest.fixture
async def api(cloud, clock):
    client = TuyaApi(cloud.config(), clock=clock)
    yield client
    await client.close()


async def test_a_scene_is_triggered_with_one_token(cloud, api):
    await api.trigger_scene(SCENE)
    await api.trigger_scene(SCENE)
    assert cloud.triggered == [SCENE, SCENE]
    assert cloud.tokens_issued == 1


async def test_a_token_is_renewed_before_it_expires(cloud, api, clock):
    await api.trigger_scene(SCENE)
    clock.now += cloud.expire_time - 30
    await api.trigger_scene(SCENE)
    assert cloud.tokens_issued == 2


async def test_a_token_the_cloud_dropped_is_replaced_once(cloud, api):
    await api.trigger_scene(SCENE)
    cloud.forget_tokens()
    await api.trigger_scene(SCENE)
    assert cloud.tokens_issued == 2
    assert cloud.triggered == [SCENE, SCENE]


async def test_a_wrong_secret_is_reported_with_the_cloud_code(cloud, clock):
    client = TuyaApi(cloud.config(secret="wrong"), clock=clock)
    try:
        with pytest.raises(TuyaError, match="1004"):
            await client.trigger_scene(SCENE)
    finally:
        await client.close()


async def test_a_cloud_error_code_is_reported(cloud, api):
    cloud.code = 1106
    with pytest.raises(TuyaError, match="1106.*permission deny"):
        await api.trigger_scene(SCENE)


async def test_an_http_error_is_reported(cloud, api):
    await api.trigger_scene(SCENE)
    cloud.status = 502
    with pytest.raises(TuyaError, match="HTTP 502"):
        await api.trigger_scene(SCENE)


async def test_a_scene_the_cloud_did_not_trigger_is_an_error(cloud, api):
    cloud.result = False
    with pytest.raises(TuyaError, match="not triggered"):
        await api.trigger_scene(SCENE)


async def test_an_unreachable_cloud_is_reported(clock):
    client = TuyaApi(TuyaConfig(ACCESS_ID, SECRET, api_url="http://127.0.0.1:9", timeout=1), clock=clock)
    try:
        with pytest.raises(TuyaError, match="requesting /v1.0/token"):
            await client.trigger_scene(SCENE)
    finally:
        await client.close()


async def test_every_call_says_it_is_json(cloud, api):
    await api.trigger_scene(SCENE)
    assert cloud.content_types == ["application/json", "application/json"]


async def test_concurrent_calls_share_one_token(cloud, api):
    await asyncio.gather(api.trigger_scene(SCENE), api.trigger_scene(SCENE))
    assert cloud.tokens_issued == 1


async def test_a_bad_token_lifetime_is_a_tuya_error(cloud, api):
    cloud.issue_bad_expiry()
    with pytest.raises(TuyaError, match="expire_time"):
        await api.trigger_scene(SCENE)


async def test_a_closed_client_makes_no_calls(cloud, api):
    await api.close()
    with pytest.raises(TuyaError, match="closed"):
        await api.trigger_scene(SCENE)
    assert cloud.tokens_issued == 0


async def test_transport_errors_do_not_carry_the_request_headers(clock):
    async def bad_reply(reader, writer):
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nbroken header line\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(bad_reply, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = TuyaApi(TuyaConfig(ACCESS_ID, SECRET, api_url=f"http://127.0.0.1:{port}", timeout=2), clock=clock)
    try:
        with pytest.raises(TuyaError) as raised:
            await client.trigger_scene(SCENE)
        assert ACCESS_ID not in str(raised.value)
        assert "client_id" not in str(raised.value)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
