"""LuxPower dongle framing (TCP port 8000, "A1 1A" packets).

References: celsworth/lxp-bridge src/lxp/packet.rs, joyfulhouse/pylxpweb transports/dongle.py.
"""

import struct
from dataclasses import dataclass

HEADER = b"\xa1\x1a"
SERIAL_LEN = 10
TCP_FUNC_HEARTBEAT = 0xC1
TCP_FUNC_TRANSLATED = 0xC2
FUNC_READ_HOLDING = 0x03
FUNC_READ_INPUT = 0x04
MODBUS_EXCEPTION_FLAG = 0x80

_PROTOCOL_READ = 1
_FRAME_MARKER = 0x01
_ACTION_REQUEST = 0
_ACTION_RESPONSE = 1
_PREFIX_LEN = 6  # header, protocol, frame length
_SERIAL_END = _PREFIX_LEN + 2 + SERIAL_LEN
# A 125-register response is ~270 bytes; anything far larger is a false header match.
_MAX_FRAME_LEN = 512


class FrameError(Exception):
    pass


@dataclass(frozen=True)
class Frame:
    tcp_func: int
    dongle_serial: str
    payload: bytes


@dataclass(frozen=True)
class ReadResponse:
    func: int
    inverter_serial: str
    start: int
    values: tuple[int, ...]
    exception: bool = False


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _serial_bytes(serial: str) -> bytes:
    raw = serial.encode("ascii")
    if len(raw) != SERIAL_LEN:
        raise ValueError(f"serial must be {SERIAL_LEN} ASCII characters: {serial!r}")
    return raw


def build_read_request(
    dongle_serial: str, inverter_serial: str, func: int, start: int, count: int
) -> bytes:
    data_frame = (
        struct.pack("<BB", _ACTION_REQUEST, func)
        + _serial_bytes(inverter_serial)
        + struct.pack("<HH", start, count)
    )
    data = data_frame + struct.pack("<H", crc16_modbus(data_frame))
    body = (
        struct.pack("<BB", _FRAME_MARKER, TCP_FUNC_TRANSLATED)
        + _serial_bytes(dongle_serial)
        + struct.pack("<H", len(data))
        + data
    )
    return HEADER + struct.pack("<HH", _PROTOCOL_READ, len(body)) + body


def split_frames(buffer: bytearray) -> list[Frame]:
    frames = []
    while True:
        start = buffer.find(HEADER)
        if start < 0:
            # Keep the last byte: it may be the first half of a header.
            del buffer[: max(len(buffer) - 1, 0)]
            return frames
        del buffer[:start]
        if len(buffer) < _PREFIX_LEN + 1:
            return frames
        (frame_len,) = struct.unpack_from("<H", buffer, 4)
        if frame_len > _MAX_FRAME_LEN or buffer[_PREFIX_LEN] != _FRAME_MARKER:
            del buffer[:1]
            continue
        total = _PREFIX_LEN + frame_len
        if len(buffer) < total:
            return frames
        raw = bytes(buffer[:total])
        del buffer[:total]
        if total >= _SERIAL_END:
            frames.append(
                Frame(
                    tcp_func=raw[_PREFIX_LEN + 1],
                    dongle_serial=raw[_PREFIX_LEN + 2 : _SERIAL_END].decode("ascii", "replace"),
                    payload=raw[_SERIAL_END:],
                )
            )


def parse_read_response(frame: Frame) -> ReadResponse | None:
    if frame.tcp_func != TCP_FUNC_TRANSLATED or len(frame.payload) < 2:
        return None
    (data_len,) = struct.unpack_from("<H", frame.payload, 0)
    data = frame.payload[2 : 2 + data_len]
    if len(data) != data_len or data_len < 4:
        raise FrameError("truncated data frame")
    data_frame = data[:-2]
    (crc,) = struct.unpack("<H", data[-2:])
    if crc16_modbus(data_frame) != crc:
        raise FrameError("CRC mismatch")
    if len(data_frame) < 14 or data_frame[0] != _ACTION_RESPONSE:
        return None
    func = data_frame[1]
    inverter_serial = data_frame[2:12].decode("ascii", "replace")
    (start,) = struct.unpack_from("<H", data_frame, 12)
    if func & MODBUS_EXCEPTION_FLAG:
        return ReadResponse(func & 0x7F, inverter_serial, start, (), exception=True)
    if len(data_frame) < 15:
        raise FrameError("read response without a byte count")
    byte_count = data_frame[14]
    if len(data_frame) < 15 + byte_count:
        raise FrameError("read response shorter than its byte count")
    values = struct.unpack_from(f"<{byte_count // 2}H", data_frame, 15)
    return ReadResponse(func, inverter_serial, start, values)
