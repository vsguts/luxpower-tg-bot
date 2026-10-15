import re

from tests.fake_dongle import FakeDongle
from tests.fixtures import DONGLE_SERIAL, INVERTER_SERIAL
from tools.dump_registers import dump


async def test_dump_prints_raw_and_decoded_registers(capsys):
    async with FakeDongle({0: 0x40, 4: 512, 5: (98 << 8) | 76, 90: 7}) as fake:
        await dump({
            "DONGLE_HOST": "127.0.0.1",
            "DONGLE_PORT": str(fake.port),
            "DONGLE_SERIAL": DONGLE_SERIAL,
            "INVERTER_SERIAL": INVERTER_SERIAL,
        })
    out = capsys.readouterr().out
    assert re.search(r"^\s+90\s+7\s+0x0007$", out, re.MULTILINE)
    assert re.search(r"^\s+soc\s+76$", out, re.MULTILINE)
    assert "battery off-grid" in out
    assert [start for _, start, _ in fake.requests] == [0, 40, 80, 120, 160]
