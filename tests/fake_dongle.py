import asyncio
import struct

from lxbot.luxpower.client import DongleConfig
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


_REQUEST_LEN = 38


class FakeDongle:
    """Local TCP server that answers read requests from a register table."""

    def __init__(self, registers: dict[int, int] | None = None):
        self.registers = registers if registers is not None else {}
        self.on_connect = b""
        self.before_reply = b""
        self.silent = False
        self.close_after_requests: int | None = None
        self.requests: list[tuple[int, int, int]] = []
        self.connections = 0
        self._writers: list[asyncio.StreamWriter] = []

    def config(self, timeout: float = 1.0) -> DongleConfig:
        return DongleConfig("127.0.0.1", self.port, DONGLE_SERIAL, INVERTER_SERIAL, timeout)

    async def __aenter__(self) -> "FakeDongle":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info) -> None:
        for writer in self._writers:
            writer.close()
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        writer.write(self.on_connect)
        try:
            while True:
                raw = await reader.readexactly(_REQUEST_LEN)
                func = raw[21]
                start, count = struct.unpack_from("<HH", raw, 32)
                self.requests.append((func, start, count))
                if self.close_after_requests is not None and len(self.requests) >= self.close_after_requests:
                    self.close_after_requests = None
                    break
                if self.silent:
                    continue
                values = [self.registers.get(reg, 0) for reg in range(start, start + count)]
                writer.write(self.before_reply + make_read_response(start, values, func))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
