import struct

import pytest

from lxbot.luxpower.frame import (
    FUNC_READ_HOLDING,
    FUNC_READ_INPUT,
    TCP_FUNC_HEARTBEAT,
    FrameError,
    build_read_request,
    crc16_modbus,
    parse_read_response,
    split_frames,
)
from tests.fake_dongle import make_frame
from tests.fixtures import (
    DONGLE_SERIAL,
    HEARTBEAT,
    HOLDING_0_80,
    INPUT_0_40,
    INVERTER_SERIAL,
    REQUEST_INPUT_0_40,
)


def test_crc16_modbus_check_value():
    assert crc16_modbus(b"123456789") == 0x4B37


def test_build_read_request_matches_reference_bytes():
    request = build_read_request(DONGLE_SERIAL, INVERTER_SERIAL, FUNC_READ_INPUT, 0, 40)
    assert request == REQUEST_INPUT_0_40


def test_build_read_request_rejects_bad_serial():
    with pytest.raises(ValueError):
        build_read_request("DG1", INVERTER_SERIAL, FUNC_READ_INPUT, 0, 40)


def test_split_frames_splits_concatenated_frames_and_empties_buffer():
    buffer = bytearray(HEARTBEAT + INPUT_0_40)
    frames = split_frames(buffer)
    assert [f.tcp_func for f in frames] == [TCP_FUNC_HEARTBEAT, 0xC2]
    assert frames[0].dongle_serial == DONGLE_SERIAL
    assert buffer == bytearray()


def test_split_frames_drops_garbage_before_header():
    buffer = bytearray(b"\x00\xff\x13" + HEARTBEAT)
    assert len(split_frames(buffer)) == 1


def test_split_frames_waits_for_the_rest_of_a_partial_frame():
    buffer = bytearray(INPUT_0_40[:30])
    assert split_frames(buffer) == []
    buffer.extend(INPUT_0_40[30:])
    assert len(split_frames(buffer)) == 1


def test_split_frames_resyncs_after_a_false_header():
    # A stray "A1 1A" followed by the next frame's header reads as a 6817-byte frame.
    buffer = bytearray(b"\xa1\x1a\x01\x00" + HEARTBEAT)
    frames = split_frames(buffer)
    assert [f.tcp_func for f in frames] == [TCP_FUNC_HEARTBEAT]


def test_parse_input_response():
    (frame,) = split_frames(bytearray(INPUT_0_40))
    response = parse_read_response(frame)
    assert response.func == FUNC_READ_INPUT
    assert response.inverter_serial == INVERTER_SERIAL
    assert response.start == 0
    assert len(response.values) == 40
    assert response.values[0] == 0x40
    assert response.values[5] & 0xFF == 76
    assert response.values[11] == 1200
    assert not response.exception


def test_parse_real_holding_capture():
    (frame,) = split_frames(bytearray(HOLDING_0_80))
    response = parse_read_response(frame)
    assert response.func == FUNC_READ_HOLDING
    assert len(response.values) == 80
    assert response.values[19] == 6


def test_parse_heartbeat_is_none():
    (frame,) = split_frames(bytearray(HEARTBEAT))
    assert parse_read_response(frame) is None


def test_parse_request_echo_is_none():
    (frame,) = split_frames(bytearray(REQUEST_INPUT_0_40))
    assert parse_read_response(frame) is None


def test_parse_bad_crc_raises():
    corrupted = bytearray(INPUT_0_40)
    corrupted[-1] ^= 0xFF
    (frame,) = split_frames(corrupted)
    with pytest.raises(FrameError):
        parse_read_response(frame)


def test_parse_modbus_exception():
    data_frame = struct.pack("<BB", 1, 0x84) + INVERTER_SERIAL.encode() + struct.pack("<HB", 0, 2)
    (frame,) = split_frames(bytearray(make_frame(data_frame)))
    response = parse_read_response(frame)
    assert response.exception
    assert response.func == FUNC_READ_INPUT
    assert response.values == ()
