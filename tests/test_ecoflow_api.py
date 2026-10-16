import hashlib
import hmac

import pytest

from lxbot.ecoflow.api import EcoFlowApi, EcoFlowConfig, EcoFlowError, signature
from tests.fake_ecoflow import ACCESS_KEY, SECRET_KEY, SERIAL, FakeCloud


def test_signature_matches_the_documented_example():
    params = {"sn": "123456789", "params": {"cmdSet": 11, "id": 24, "eps": 0}}
    sign = signature(params, "Fp4SvIprYSDPXtYJidEtUAd1o", "WIbFEKre0s6sLnh4ei7SPUeYnptHG6V", "345164", "1671171709428")
    assert sign == "07c13b65e037faf3b153d51613638fa80003c4c38d2407379a7f52851af1473e"


def test_signature_without_params_covers_only_the_credentials():
    expected = hmac.new(b"s", b"accessKey=a&nonce=1&timestamp=2", hashlib.sha256).hexdigest()
    assert signature({}, "a", "s", "1", "2") == expected


def test_config_defaults_to_the_european_cloud_and_hides_the_secret():
    config = EcoFlowConfig(ACCESS_KEY, SECRET_KEY, SERIAL)
    assert config.api_url == "https://api-e.ecoflow.com"
    assert SECRET_KEY not in repr(config)


@pytest.fixture
async def cloud():
    async with FakeCloud() as fake:
        yield fake


@pytest.fixture
async def api(cloud):
    client = EcoFlowApi(cloud.config())
    yield client
    await client.close()


async def test_devices_are_listed(api):
    assert await api.devices() == [{"sn": SERIAL, "online": 1, "productName": "DELTA Pro"}]


async def test_quota_of_the_configured_serial(api):
    assert await api.quota() == {"bmsMaster.soc": 87, "inv.acInVol": 0}


async def test_wrong_secret_is_reported_with_the_cloud_code(cloud):
    client = EcoFlowApi(cloud.config(secret_key="wrong"))
    try:
        with pytest.raises(EcoFlowError, match="8521"):
            await client.devices()
    finally:
        await client.close()


async def test_cloud_error_code(cloud, api):
    cloud.code = "1006"
    with pytest.raises(EcoFlowError, match="1006.*boom"):
        await api.quota()


async def test_http_error(cloud, api):
    cloud.status = 502
    with pytest.raises(EcoFlowError, match="HTTP 502"):
        await api.quota()


async def test_quota_that_is_not_an_object(cloud, api):
    cloud.quota = [1, 2]
    with pytest.raises(EcoFlowError, match="not a JSON object"):
        await api.quota()


async def test_device_list_that_is_not_an_array(cloud, api):
    cloud.devices = {"sn": SERIAL}
    with pytest.raises(EcoFlowError, match="not a JSON array"):
        await api.devices()


async def test_unreachable_cloud():
    client = EcoFlowApi(EcoFlowConfig(ACCESS_KEY, SECRET_KEY, SERIAL, api_url="http://127.0.0.1:9", timeout=2))
    try:
        with pytest.raises(EcoFlowError):
            await client.devices()
    finally:
        await client.close()
