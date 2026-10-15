import struct

from lxbot.luxpower.frame import HEADER, crc16_modbus
from tests.fixtures import DONGLE_SERIAL, INVERTER_SERIAL


def make_frame(data_frame: bytes, tcp_func: int = 0xC2) -> bytes:
    data = data_frame + struct.pack("<H", crc16_modbus(data_frame))
    body = bytes([1, tcp_func]) + DONGLE_SERIAL.encode() + struct.pack("<H", len(data)) + data
    return HEADER + struct.pack("<HH", 1, len(body)) + body


def make_read_response(start: int, values: list[int], func: int = 4) -> bytes:
    data_frame = (
        struct.pack("<BB", 1, func)
        + INVERTER_SERIAL.encode()
        + struct.pack("<HB", start, 2 * len(values))
        + struct.pack(f"<{len(values)}H", *values)
    )
    return make_frame(data_frame)
