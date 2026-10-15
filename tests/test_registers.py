import pytest

from lxbot.luxpower.frame import parse_read_response, split_frames
from lxbot.luxpower.registers import InvalidSnapshot, decode_snapshot
from tests.factories import BASE, make_snapshot
from tests.fixtures import INPUT_0_40


def _registers(**overrides: int) -> dict[int, int]:
    (frame,) = split_frames(bytearray(INPUT_0_40))
    registers = dict.fromkeys(range(200), 0)
    registers.update(enumerate(parse_read_response(frame).values))
    registers.update({int(k[1:]): v for k, v in overrides.items()})
    return registers


def test_decode_fixture_block():
    snapshot = decode_snapshot(_registers(), BASE)
    assert snapshot.taken_at == BASE
    assert snapshot.status == 0x40
    assert snapshot.status_name == "battery off-grid"
    assert snapshot.soc == 76
    assert snapshot.battery_v == pytest.approx(51.2)
    assert snapshot.battery_w == -1200
    assert snapshot.pv_w == 500
    assert snapshot.eps_v == pytest.approx(230.1)
    assert snapshot.eps_hz == pytest.approx(50.0)
    assert snapshot.eps_w == 850
    assert not snapshot.grid_present
    assert not snapshot.gen_running


def test_decode_grid_generator_and_signed_load():
    snapshot = decode_snapshot(
        _registers(r12=2290, r15=5001, r77=1, r121=2310, r122=4990, r170=0xFFF6), BASE
    )
    assert snapshot.grid_v == pytest.approx(229.0)
    assert snapshot.grid_hz == pytest.approx(50.01)
    assert snapshot.ac_source_gen
    assert snapshot.gen_v == pytest.approx(231.0)
    assert snapshot.load_w == -10


def test_decode_rejects_empty_block():
    with pytest.raises(InvalidSnapshot):
        decode_snapshot(dict.fromkeys(range(200), 0), BASE)


def test_decode_rejects_soc_above_100():
    with pytest.raises(InvalidSnapshot):
        decode_snapshot(_registers(r5=(98 << 8) | 250), BASE)


@pytest.mark.parametrize(
    ("grid_v", "grid_hz", "expected"),
    [(230.0, 50.0, True), (0.0, 0.0, False), (150.0, 50.0, False), (230.0, 0.0, False)],
)
def test_grid_present(grid_v, grid_hz, expected):
    assert make_snapshot(grid_v=grid_v, grid_hz=grid_hz).grid_present is expected


def test_gen_running_from_generator_voltage():
    assert make_snapshot(grid=False, gen=True).gen_running


def test_gen_running_from_source_bit_while_ac_input_present():
    snapshot = make_snapshot(grid=False, status=0x11, ac_source_gen=True)
    assert snapshot.gen_running


def test_source_bit_alone_is_not_a_running_generator():
    snapshot = make_snapshot(grid=False, status=0x40, ac_source_gen=True)
    assert not snapshot.gen_running
