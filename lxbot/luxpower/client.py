import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime

from lxbot.luxpower.frame import (
    FUNC_READ_INPUT,
    FrameError,
    build_read_request,
    parse_read_response,
    split_frames,
)
from lxbot.luxpower.registers import BLOCK_SIZE, INPUT_BLOCKS, InvalidSnapshot, Snapshot, decode_snapshot

log = logging.getLogger(__name__)

_READ_CHUNK = 4096
_DRAIN_TIMEOUT = 0.05


class DongleError(Exception):
    pass


@dataclass(frozen=True)
class DongleConfig:
    host: str
    port: int
    dongle_serial: str
    inverter_serial: str
    timeout: float = 5.0


class DongleClient:
    def __init__(self, config: DongleConfig):
        self._config = config
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer = bytearray()

    async def read_snapshot(self, taken_at: datetime) -> Snapshot:
        registers: dict[int, int] = {}
        for start in INPUT_BLOCKS:
            registers.update(enumerate(await self.read_input(start, BLOCK_SIZE), start))
        try:
            return decode_snapshot(registers, taken_at)
        except InvalidSnapshot as exc:
            await self.close()
            raise DongleError(str(exc)) from exc

    async def read_input(self, start: int, count: int) -> tuple[int, ...]:
        try:
            return await asyncio.wait_for(
                self._read(FUNC_READ_INPUT, start, count), self._config.timeout
            )
        except (OSError, TimeoutError, DongleError) as exc:
            await self.close()
            raise DongleError(f"reading input registers {start}+{count}: {exc!r}") from exc

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _read(self, func: int, start: int, count: int) -> tuple[int, ...]:
        reader, writer = await self._connect()
        await self._drain(reader)
        writer.write(
            build_read_request(
                self._config.dongle_serial, self._config.inverter_serial, func, start, count
            )
        )
        await writer.drain()
        expected = (func, self._config.inverter_serial, start)
        while True:
            chunk = await reader.read(_READ_CHUNK)
            if not chunk:
                raise DongleError("connection closed by the dongle")
            self._buffer.extend(chunk)
            for frame in split_frames(self._buffer):
                try:
                    response = parse_read_response(frame)
                except FrameError as exc:
                    log.warning("skipping a bad frame: %s", exc)
                    continue
                if response is None or (response.func, response.inverter_serial, response.start) != expected:
                    continue
                if response.exception:
                    raise DongleError(f"Modbus exception reading register {start}")
                if len(response.values) != count:
                    # The cloud can read the same start register with another count on the shared socket.
                    log.warning("skipping a reply with %d registers, expected %d", len(response.values), count)
                    continue
                return response.values

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            self._reader, self._writer = await asyncio.open_connection(
                self._config.host, self._config.port
            )
            self._buffer.clear()
        return self._reader, self._writer

    async def _drain(self, reader: asyncio.StreamReader) -> None:
        # Cloud replies and heartbeats share the socket; anything queued now is not ours.
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(_READ_CHUNK), _DRAIN_TIMEOUT)
            except TimeoutError:
                break
            if not chunk:
                raise DongleError("connection closed by the dongle")
        self._buffer.clear()
