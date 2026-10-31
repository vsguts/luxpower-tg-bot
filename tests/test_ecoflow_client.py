import json
from pathlib import Path

import pytest

from lxbot.device.snapshot import DeviceError
from lxbot.ecoflow.client import EcoFlowClient
from tests.factories import BASE
from tests.fake_ecoflow import SERIAL, FakeCloud

GRID_ON = json.loads((Path(__file__).parent / "fixtures" / "ecoflow-delta-pro-grid-on.json").read_text())


@pytest.fixture
async def cloud():
    async with FakeCloud() as fake:
        fake.quota = GRID_ON
        yield fake


@pytest.fixture
async def client(cloud):
    ecoflow = EcoFlowClient(cloud.config())
    yield ecoflow
    await ecoflow.close()


async def test_reads_a_snapshot_of_an_online_station(client):
    snapshot = await client.read_snapshot(BASE)
    assert (snapshot.taken_at, snapshot.soc, snapshot.grid_present) == (BASE, 100, True)


async def test_offline_station_is_a_device_error_not_its_last_values(cloud, client):
    # The cloud keeps answering with the last quota it saw: the grid would look present forever.
    cloud.devices = [{"sn": SERIAL, "online": 0, "productName": "DELTA Pro"}]
    with pytest.raises(DeviceError, match="offline"):
        await client.read_snapshot(BASE)


async def test_station_missing_from_the_account(cloud, client):
    cloud.devices = []
    with pytest.raises(DeviceError, match="not bound"):
        await client.read_snapshot(BASE)


async def test_invalid_quota_is_a_device_error(cloud, client):
    cloud.quota = {"pd.soc": 50}
    with pytest.raises(DeviceError, match="inv.acInVol|bmsMaster"):
        await client.read_snapshot(BASE)


async def test_cloud_failure_is_a_device_error(cloud, client):
    cloud.status = 502
    with pytest.raises(DeviceError, match="HTTP 502"):
        await client.read_snapshot(BASE)
