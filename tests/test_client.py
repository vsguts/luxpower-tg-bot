import pytest

from lxbot.luxpower.client import DongleClient, DongleError
from tests.factories import BASE
from tests.fake_dongle import FakeDongle, make_read_response
from tests.fixtures import HEARTBEAT, INPUT_0_40

SNA_REGISTERS = {0: 0x40, 4: 512, 5: (98 << 8) | 76, 11: 1200, 170: 0xFFF6}


@pytest.fixture
async def dongle():
    async with FakeDongle(dict(SNA_REGISTERS)) as fake:
        yield fake


@pytest.fixture
async def client(dongle):
    client = DongleClient(dongle.config(timeout=0.3))
    yield client
    await client.close()


async def test_read_input(dongle, client):
    values = await client.read_input(0, 40)
    assert len(values) == 40
    assert values[0] == 0x40
    assert dongle.requests == [(4, 0, 40)]


async def test_read_snapshot_reads_all_blocks(dongle, client):
    snapshot = await client.read_snapshot(BASE)
    assert snapshot.soc == 76
    assert snapshot.battery_w == -1200
    assert snapshot.load_w == -10
    assert [start for _, start, _ in dongle.requests] == [0, 40, 120, 160]


async def test_skips_heartbeats_cloud_replies_and_bad_frames(dongle, client):
    corrupted = bytearray(INPUT_0_40)
    corrupted[-1] ^= 0xFF
    dongle.before_reply = (
        HEARTBEAT
        + b"\x00\xff"
        + make_read_response(0, [9] * 80, func=3)
        + make_read_response(40, [9] * 40)
        + bytes(corrupted)
    )
    values = await client.read_input(0, 40)
    assert values[0] == 0x40


async def test_drains_stale_data_queued_before_the_request(dongle, client):
    dongle.on_connect = make_read_response(0, [7] * 40)
    values = await client.read_input(0, 40)
    assert values[0] == 0x40


async def test_timeout_raises_and_next_call_reconnects(dongle, client):
    dongle.silent = True
    with pytest.raises(DongleError):
        await client.read_input(0, 40)
    dongle.silent = False
    assert (await client.read_input(0, 40))[0] == 0x40
    assert dongle.connections == 2


async def test_closed_connection_raises_and_next_call_reconnects(dongle, client):
    dongle.close_after_requests = 1
    with pytest.raises(DongleError):
        await client.read_input(0, 40)
    assert (await client.read_input(0, 40))[0] == 0x40
    assert dongle.connections == 2


async def test_invalid_snapshot_is_a_dongle_error(dongle, client):
    dongle.registers.clear()
    with pytest.raises(DongleError):
        await client.read_snapshot(BASE)


async def test_unreachable_dongle_raises():
    async with FakeDongle() as fake:
        config = fake.config(timeout=0.3)
    client = DongleClient(config)
    with pytest.raises(DongleError):
        await client.read_input(0, 40)
