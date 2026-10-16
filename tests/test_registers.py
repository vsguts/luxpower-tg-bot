import pytest

from lxbot.luxpower.frame import parse_read_response, split_frames
from lxbot.device.snapshot import Snapshot
from lxbot.luxpower.registers import InvalidReading, decode_reading
from tests.factories import BASE
from tests.fixtures import INPUT_0_40


def _registers(**overrides: int) -> dict[int, int]:
    (frame,) = split_frames(bytearray(INPUT_0_40))
    registers = dict.fromkeys(range(200), 0)
    registers.update(enumerate(parse_read_response(frame).values))
    registers.update({int(k[1:]): v for k, v in overrides.items()})
    return registers


def test_decode_fixture_block():
    reading = decode_reading(_registers(), BASE)
    assert reading.taken_at == BASE
    assert reading.status == 0x40
    assert reading.status_name == "battery off-grid"
    assert reading.soc == 76
    assert reading.battery_v == pytest.approx(51.2)
    assert reading.battery_w == -1200
    assert reading.pv_w == 500
    assert reading.eps_v == pytest.approx(230.1)
    assert reading.eps_hz == pytest.approx(50.0)
    assert reading.eps_w == 850
    assert not reading.to_snapshot().grid_present
    assert not reading.gen_running


def test_decode_grid_generator_and_signed_load():
    reading = decode_reading(
        _registers(r12=2290, r15=5001, r77=1, r121=2310, r122=4990, r170=0xFFF6), BASE
    )
    assert reading.grid_v == pytest.approx(229.0)
    assert reading.grid_hz == pytest.approx(50.01)
    assert reading.ac_source_gen
    assert reading.gen_v == pytest.approx(231.0)
    assert reading.load_w == -10


def test_decode_rejects_empty_block():
    with pytest.raises(InvalidReading):
        decode_reading(dict.fromkeys(range(200), 0), BASE)


def test_decode_rejects_soc_above_100():
    with pytest.raises(InvalidReading):
        decode_reading(_registers(r5=(98 << 8) | 250), BASE)


def test_gen_running_from_generator_voltage():
    assert decode_reading(_registers(r121=2310, r122=4990), BASE).gen_running


def test_gen_running_from_source_bit_while_ac_input_present():
    assert decode_reading(_registers(r0=0x11, r77=1), BASE).gen_running


def test_source_bit_alone_is_not_a_running_generator():
    assert not decode_reading(_registers(r0=0x40, r77=1), BASE).gen_running


def test_reading_converts_to_the_shared_snapshot():
    snapshot = decode_reading(_registers(r12=2290, r15=5001, r121=2310, r122=4990), BASE).to_snapshot()
    assert snapshot == Snapshot(
        taken_at=BASE, soc=76, battery_w=-1200, pv_w=500, load_w=0, grid_v=229.0, grid_hz=50.01, gen_running=True
    )
