import json

import pytest

from lxbot.config import ConfigError
from tests.fake_ecoflow import ACCESS_KEY, SECRET_KEY, SERIAL, FakeCloud
from tools.dump_ecoflow import dump


def _env(cloud: FakeCloud) -> dict[str, str]:
    return {
        "ECOFLOW_ACCESS_KEY": ACCESS_KEY,
        "ECOFLOW_SECRET_KEY": SECRET_KEY,
        "ECOFLOW_SERIAL": SERIAL,
        "ECOFLOW_API_URL": cloud.url,
    }


async def test_dump_prints_the_quota_as_json_and_the_device_state_aside(capsys):
    async with FakeCloud() as cloud:
        assert await dump(_env(cloud)) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"bmsMaster.soc": 87, "inv.acInVol": 0}
    assert "DELTA Pro: online" in err


async def test_dump_warns_that_an_offline_quota_is_stale(capsys):
    async with FakeCloud() as cloud:
        cloud.devices = [{"sn": SERIAL, "online": 0, "productName": "DELTA Pro"}]
        assert await dump(_env(cloud)) == 0
    assert "OFFLINE" in capsys.readouterr().err


async def test_dump_lists_the_bound_devices_when_the_serial_is_not_one_of_them(capsys):
    async with FakeCloud() as cloud:
        cloud.devices = [{"sn": "R331ZEB4ZE000001", "online": 1, "productName": "RIVER 2"}]
        assert await dump(_env(cloud)) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert SERIAL in err and "R331ZEB4ZE000001 (RIVER 2)" in err


async def test_dump_requires_the_credentials():
    with pytest.raises(ConfigError, match="ECOFLOW_SECRET_KEY, ECOFLOW_SERIAL"):
        await dump({"ECOFLOW_ACCESS_KEY": ACCESS_KEY})
