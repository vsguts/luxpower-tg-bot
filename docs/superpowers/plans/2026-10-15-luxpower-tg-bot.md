# LuxPower Telegram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Telegram bot (Ukrainian UI) that polls a LuxPower SNA6000 WPV through its WiFi dongle
and notifies approved subscribers about grid outages, generator use and battery level, with `/status`
on demand.

**Architecture:** One asyncio Python service: a small dongle client (TCP 8000, "A1 1A" frames)
produces `Snapshot`s, a pure detector turns them into events, a notifier journals events to SQLite
and queues Telegram messages, and aiogram handles subscriptions and `/status`. One Docker container
on rock2.

**Tech Stack:** Python 3.12, aiogram 3, aiosqlite, tzdata, pytest + pytest-asyncio, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-15-luxpower-tg-bot-design.md`

## Global Constraints

- Python 3.12 (`python:3.12-slim` in Docker, `python3.12` venv locally). Runtime dependencies are
  only `aiogram>=3.15,<4`, `aiosqlite>=0.20,<1`, `tzdata`; dev adds `pytest>=8`, `pytest-asyncio>=0.24`.
- All code, comments and docs in English; every user-facing bot text in Ukrainian.
- The repository is **public**: no real serials, IP addresses, tokens or chat ids in git. Test
  fixtures use dongle `DG00000001` and inverter `0000000001`.
- Read-only towards the inverter: the client has no write function.
- The bot is the only local client of the dongle.
- Timestamps are timezone-aware UTC internally; messages show `Europe/Kyiv` local time.
- Thresholds: `SOC_LOW=30` (re-armed at 35), `SOC_CRITICAL=15` (re-armed at 20), `SOC_GEN_FULL=90`;
  a grid or generator change needs 3 consecutive polls at `POLL_INTERVAL=15` s.
- Comments explain only *why*; no doc blocks on obvious functions.
- Commit with explicit paths (`git add <files>`), never `git add -A`. Commit messages end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Empty or garbled register block** (dongle returns zeros while the inverter is busy): expect no
   battery alert. A snapshot with battery < 30 V or SOC > 100 is rejected → Task 2 test
   `test_decode_rejects_empty_block`.
2. **Telegram unreachable** (ISP hiccup during an outage): expect polling and journaling to keep
   going while messages wait → Task 7 test `test_publish_does_not_wait_for_delivery`.
3. **Bot restarted mid-outage** (container or rock2 reboot): expect no second "Світло зникло" and a
   restore message with the full outage duration → Task 8 test `test_restart_mid_outage_keeps_outage_start`.
4. **Admin taps ✅ twice** (or ✅ after ❌): expect the second tap to be a no-op → Task 10 test
   `test_decide_twice_is_noop`.
5. **User name with `<` or `&`** in an HTML-mode admin card: expect the card to be sent, escaped →
   Task 6 test `test_admin_request_text_escapes_html`.

## File Structure

```
lxbot/
  __init__.py
  config.py                 Settings and DongleConfig from environment
  main.py                   wires everything, entry point
  luxpower/
    __init__.py
    frame.py                CRC, request builder, frame splitter, response parser (pure)
    registers.py            SNA register map, Snapshot, decode_snapshot (pure)
    client.py               DongleConfig, DongleClient (TCP I/O), DongleError
  monitor/
    __init__.py
    detector.py             EventType, Event, Thresholds, Debounced, State, detect (pure)
    watchdog.py             DongleWatch: dongle down/up events (pure)
    poller.py               Poller: source -> detector -> storage -> notifier
  storage/
    __init__.py
    db.py                   Database (aiosqlite): subscribers, events, state
  notify/
    __init__.py
    broadcast.py            Notifier: journal + queued Telegram delivery with retries
  bot/
    __init__.py
    texts.py                Ukrainian texts and formatters (pure)
    subscriptions.py        subscription rules over Database
    handlers.py             aiogram Router
tools/
  __init__.py
  dump_registers.py         calibration dump
tests/
  __init__.py
  fixtures.py               anonymised protocol frames (hex)
  fake_dongle.py            frame helpers + local fake dongle server
  factories.py              make_snapshot, at()
  conftest.py               db fixture, make_settings
  test_frame.py test_registers.py test_client.py test_detector.py test_watchdog.py
  test_db.py test_texts.py test_notifier.py test_poller.py test_config.py
  test_subscriptions.py test_handlers.py test_dump_registers.py
pyproject.toml requirements.txt requirements-dev.txt .gitignore .dockerignore
Dockerfile compose.yaml .env.example README.md
```

Run all commands from the repository root `~/Projects/vsguts/luxpower-tg-bot`.

---

### Task 1: Project scaffold and frame codec

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.gitignore`
- Create: `lxbot/__init__.py`, `lxbot/luxpower/__init__.py` (both empty)
- Create: `lxbot/luxpower/frame.py`
- Create: `tests/__init__.py` (empty), `tests/fixtures.py`, `tests/fake_dongle.py`
- Test: `tests/test_frame.py`

**Interfaces:**
- Produces (`lxbot.luxpower.frame`): `HEADER: bytes`, `SERIAL_LEN = 10`, `TCP_FUNC_HEARTBEAT = 0xC1`,
  `TCP_FUNC_TRANSLATED = 0xC2`, `FUNC_READ_HOLDING = 0x03`, `FUNC_READ_INPUT = 0x04`,
  `class FrameError(Exception)`, `Frame(tcp_func: int, dongle_serial: str, payload: bytes)`,
  `ReadResponse(func: int, inverter_serial: str, start: int, values: tuple[int, ...], exception: bool = False)`,
  `crc16_modbus(data: bytes) -> int`,
  `build_read_request(dongle_serial: str, inverter_serial: str, func: int, start: int, count: int) -> bytes`,
  `split_frames(buffer: bytearray) -> list[Frame]` (consumes complete frames from the buffer),
  `parse_read_response(frame: Frame) -> ReadResponse | None`.
- Produces (`tests.fixtures`): `DONGLE_SERIAL`, `INVERTER_SERIAL`, `REQUEST_INPUT_0_40`,
  `INPUT_0_40`, `HOLDING_0_80`, `HEARTBEAT` (bytes).
- Produces (`tests.fake_dongle`): `make_frame(data_frame: bytes, tcp_func: int = 0xC2) -> bytes`,
  `make_read_response(start: int, values: list[int], func: int = 4) -> bytes`,
  `class FakeDongle` (added in Task 3).

- [ ] **Step 1: Scaffold the project**

`pyproject.toml`:

```toml
[project]
name = "luxpower-tg-bot"
version = "0.1.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
pythonpath = ["."]
testpaths = ["tests"]
```

`requirements.txt`:

```
aiogram>=3.15,<4
aiosqlite>=0.20,<1
tzdata>=2024.1
```

`requirements-dev.txt`:

```
-r requirements.txt
pytest>=8
pytest-asyncio>=0.24
```

`.gitignore`:

```
.venv/
__pycache__/
.pytest_cache/
.env
data/
```

Create empty `lxbot/__init__.py`, `lxbot/luxpower/__init__.py`, `tests/__init__.py`, then:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

- [ ] **Step 2: Add the fixtures**

`tests/fixtures.py` (frames generated with the real CRC; `HOLDING_0_80` is a real dongle capture
with the serials replaced and the CRC recomputed):

```python
DONGLE_SERIAL = "DG00000001"
INVERTER_SERIAL = "0000000001"

REQUEST_INPUT_0_40 = bytes.fromhex(
    "a11a0100200001c24447303030303030303112000004303030303030303030310000280075ae"
)

# Input registers 0-39: status 0x40, battery 51.2 V, SOC 76 / SOH 98, PV 300+200 W,
# discharge 1200 W, no grid, EPS 230.1 V 50.00 Hz 850 W.
INPUT_0_40 = bytes.fromhex(
    "a11a01006f0001c2444730303030303030316100010430303030303030303031000050400000000000000000"
    "024c6200002c01c80000000000b00400000000000000000000000000000000fd080000000088135203000000"
    "0000000000000000000000000000000000000000000000000000000dc2"
)

# Holding registers 0-79; register 19 (device type) is 6.
HOLDING_0_80 = bytes.fromhex(
    "a11a0500bf0001c244473030303030303031b1000103303030303030303030310000a0c00a01003030303030"
    "3030303031636261611d27220100001a0a0f160e31010001000000000006000400810341001e003c00a307e2"
    "095c12ba13a307e209f401d0077e04550a0a0064000000c80a0000080000005c1250140a00d7000000000000"
    "000000000000000000000000000000000000000000000064000000e803640064006400640000000000000000"
    "000000000000000000650000000000000000000e37"
)

HEARTBEAT = bytes.fromhex("a11a02000d0001c14447303030303030303100")
```

`tests/fake_dongle.py` (helpers only for now; the server comes in Task 3):

```python
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
```

- [ ] **Step 3: Write the failing tests**

`tests/test_frame.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_frame.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.luxpower.frame'`

- [ ] **Step 5: Implement `lxbot/luxpower/frame.py`**

```python
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
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_frame.py -v`
Expected: 13 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements.txt requirements-dev.txt .gitignore lxbot/__init__.py \
  lxbot/luxpower/__init__.py lxbot/luxpower/frame.py tests/__init__.py tests/fixtures.py \
  tests/fake_dongle.py tests/test_frame.py
git commit -m "Add LuxPower dongle frame codec

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: SNA register map and Snapshot

**Files:**
- Create: `lxbot/luxpower/registers.py`
- Create: `tests/factories.py`
- Test: `tests/test_registers.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (the test uses `tests.fixtures.INPUT_0_40` via Task 1's parser).
- Produces (`lxbot.luxpower.registers`): `BLOCK_SIZE = 40`, `INPUT_BLOCKS = (0, 40, 120, 160)`,
  `MIN_AC_VOLTAGE = 180.0`, `MIN_AC_FREQUENCY = 45.0`, `STATUS_NAMES: dict[int, str]`,
  `class InvalidSnapshot(ValueError)`, frozen dataclass `Snapshot` with fields
  `taken_at: datetime, status: int, soc: int, battery_v: float, battery_w: int, pv_w: int,
  grid_v: float, grid_hz: float, eps_v: float, eps_hz: float, eps_w: int, ac_source_gen: bool,
  gen_v: float, gen_hz: float, load_w: int` and properties `grid_present: bool`,
  `ac_input_present: bool`, `gen_running: bool`, `status_name: str`;
  `decode_snapshot(registers: Mapping[int, int], taken_at: datetime) -> Snapshot`.
- Produces (`tests.factories`): `BASE: datetime`, `at(poll: int) -> datetime`,
  `make_snapshot(poll: int = 0, *, grid: bool = True, gen: bool = False, soc: int = 80, **overrides) -> Snapshot`.

- [ ] **Step 1: Write the failing tests**

`tests/factories.py`:

```python
from datetime import UTC, datetime, timedelta

from lxbot.luxpower.registers import Snapshot

BASE = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)


def at(poll: int) -> datetime:
    return BASE + timedelta(seconds=15 * poll)


def make_snapshot(poll: int = 0, *, grid: bool = True, gen: bool = False, soc: int = 80, **overrides) -> Snapshot:
    fields = dict(
        taken_at=at(poll),
        status=0x11 if grid or gen else 0x40,
        soc=soc,
        battery_v=52.0,
        battery_w=0,
        pv_w=0,
        grid_v=230.0 if grid else 0.0,
        grid_hz=50.0 if grid else 0.0,
        eps_v=230.0,
        eps_hz=50.0,
        eps_w=500,
        ac_source_gen=gen and not grid,
        gen_v=230.0 if gen else 0.0,
        gen_hz=50.0 if gen else 0.0,
        load_w=500,
    )
    fields.update(overrides)
    return Snapshot(**fields)
```

`tests/test_registers.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_registers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.luxpower.registers'`

- [ ] **Step 3: Implement `lxbot/luxpower/registers.py`**

```python
"""SNA input register map (function 0x04) and the decoded Snapshot.

The map comes from EG4 off-grid units (pylxpweb, luxpower-ha-integration) and is not yet verified
on the SNA6000 WPV: confirm new fields with tools/dump_registers.py against the inverter display.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

BLOCK_SIZE = 40
INPUT_BLOCKS = (0, 40, 120, 160)

MIN_AC_VOLTAGE = 180.0
MIN_AC_FREQUENCY = 45.0
# A 48 V pack never reads this low; such values mean the dongle sent an empty block.
MIN_BATTERY_VOLTAGE = 30.0

STATUS_NAMES = {
    0x00: "standby",
    0x04: "PV on-grid",
    0x08: "PV charge",
    0x0C: "PV charge on-grid",
    0x10: "battery on-grid",
    0x11: "bypass",
    0x14: "PV & battery on-grid",
    0x19: "PV charge + bypass",
    0x20: "AC charge",
    0x28: "PV & AC charge",
    0x40: "battery off-grid",
    0x80: "PV off-grid",
    0x88: "PV charge off-grid",
    0xC0: "PV & battery off-grid",
}
AC_INPUT_STATUSES = frozenset({0x11, 0x19, 0x20, 0x28})


class Reg(IntEnum):
    STATUS = 0
    BATTERY_V = 4
    SOC_SOH = 5
    PV1_W = 7
    PV2_W = 8
    PV3_W = 9
    CHARGE_W = 10
    DISCHARGE_W = 11
    GRID_V = 12
    GRID_HZ = 15
    EPS_V = 20
    EPS_HZ = 23
    EPS_W = 24
    AC_SOURCE = 77
    GEN_V = 121
    GEN_HZ = 122
    LOAD_W = 170


class InvalidSnapshot(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    status: int
    soc: int
    battery_v: float
    battery_w: int
    pv_w: int
    grid_v: float
    grid_hz: float
    eps_v: float
    eps_hz: float
    eps_w: int
    ac_source_gen: bool
    gen_v: float
    gen_hz: float
    load_w: int

    @property
    def grid_present(self) -> bool:
        return self.grid_v > MIN_AC_VOLTAGE and self.grid_hz > MIN_AC_FREQUENCY

    @property
    def ac_input_present(self) -> bool:
        return self.status in AC_INPUT_STATUSES

    @property
    def gen_running(self) -> bool:
        gen_voltage_ok = self.gen_v > MIN_AC_VOLTAGE and self.gen_hz > MIN_AC_FREQUENCY
        return gen_voltage_ok or (self.ac_source_gen and self.ac_input_present)

    @property
    def status_name(self) -> str:
        return STATUS_NAMES.get(self.status, f"unknown {self.status:#04x}")


def _signed(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def decode_snapshot(registers: Mapping[int, int], taken_at: datetime) -> Snapshot:
    r = registers
    snapshot = Snapshot(
        taken_at=taken_at,
        status=r[Reg.STATUS],
        soc=r[Reg.SOC_SOH] & 0xFF,
        battery_v=r[Reg.BATTERY_V] / 10,
        battery_w=r[Reg.CHARGE_W] - r[Reg.DISCHARGE_W],
        pv_w=r[Reg.PV1_W] + r[Reg.PV2_W] + r[Reg.PV3_W],
        grid_v=r[Reg.GRID_V] / 10,
        grid_hz=r[Reg.GRID_HZ] / 100,
        eps_v=r[Reg.EPS_V] / 10,
        eps_hz=r[Reg.EPS_HZ] / 100,
        eps_w=r[Reg.EPS_W],
        ac_source_gen=bool(r[Reg.AC_SOURCE] & 1),
        gen_v=r[Reg.GEN_V] / 10,
        gen_hz=r[Reg.GEN_HZ] / 100,
        load_w=_signed(r[Reg.LOAD_W]),
    )
    if snapshot.battery_v < MIN_BATTERY_VOLTAGE or snapshot.soc > 100:
        raise InvalidSnapshot(f"implausible battery {snapshot.battery_v} V, SOC {snapshot.soc}%")
    return snapshot
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_registers.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add lxbot/luxpower/registers.py tests/factories.py tests/test_registers.py
git commit -m "Add SNA register map and Snapshot decoding

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Dongle client

**Files:**
- Create: `lxbot/luxpower/client.py`
- Modify: `tests/fake_dongle.py` (append `FakeDongle`)
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `build_read_request`, `split_frames`, `parse_read_response`, `FrameError`,
  `FUNC_READ_INPUT` (Task 1); `BLOCK_SIZE`, `INPUT_BLOCKS`, `decode_snapshot`, `InvalidSnapshot`,
  `Snapshot` (Task 2).
- Produces (`lxbot.luxpower.client`): `class DongleError(Exception)`,
  frozen dataclass `DongleConfig(host: str, port: int, dongle_serial: str, inverter_serial: str, timeout: float = 5.0)`,
  `class DongleClient(config: DongleConfig)` with
  `async read_input(start: int, count: int) -> tuple[int, ...]`,
  `async read_snapshot(taken_at: datetime) -> Snapshot`, `async close() -> None`.
  Every failure (connect, timeout, closed socket, Modbus exception, invalid snapshot) raises
  `DongleError` and leaves the client closed; the next call reconnects.
- Produces (`tests.fake_dongle`): `FakeDongle(registers: dict[int, int] | None = None)` async context
  manager with attributes `port`, `registers`, `on_connect: bytes`, `before_reply: bytes`,
  `silent: bool`, `close_after_requests: int | None`, `requests: list[tuple[int, int, int]]`,
  `connections: int`; and `config(timeout: float = 1.0) -> DongleConfig`.

- [ ] **Step 1: Add `FakeDongle` to `tests/fake_dongle.py`**

Add `import asyncio` to the imports and append:

```python
from lxbot.luxpower.client import DongleConfig

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
```

- [ ] **Step 2: Write the failing tests**

`tests/test_client.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.luxpower.client'`

- [ ] **Step 4: Implement `lxbot/luxpower/client.py`**

```python
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
                    raise DongleError(f"expected {count} registers, got {len(response.values)}")
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_client.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add lxbot/luxpower/client.py tests/fake_dongle.py tests/test_client.py
git commit -m "Add async dongle client

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Detector

**Files:**
- Create: `lxbot/monitor/__init__.py` (empty), `lxbot/monitor/detector.py`
- Test: `tests/test_detector.py`

**Interfaces:**
- Consumes: `Snapshot` (Task 2); `make_snapshot`, `at` (Task 2 factories).
- Produces (`lxbot.monitor.detector`):
  - `class EventType(StrEnum)`: `GRID_LOST`, `GRID_RESTORED`, `BATTERY_LOW`, `BATTERY_CRITICAL`,
    `GEN_CHARGED`, `GEN_STARTED`, `GEN_STOPPED`, `DONGLE_DOWN`, `DONGLE_UP` (values are the
    lower-case names).
  - `class Audience(StrEnum)`: `SUBSCRIBERS`, `ADMIN`, `JOURNAL`.
  - frozen dataclass `Event(type: EventType, at: datetime, soc: int | None = None,
    previous_duration: timedelta | None = None, gen_was_running: bool = False)` with property
    `audience: Audience` and `to_payload() -> dict`.
  - frozen dataclass `Thresholds(soc_low: int = 30, soc_critical: int = 15, soc_gen_full: int = 90,
    rearm_margin: int = 5, confirm_polls: int = 3)`.
  - frozen dataclass `Debounced(value: bool, since: datetime, pending_since: datetime | None = None,
    pending_count: int = 0)` with `observe(observed: bool, at: datetime, confirm_polls: int) -> Debounced`.
  - frozen dataclass `State(grid: Debounced, gen: Debounced, low_sent: bool = False,
    critical_sent: bool = False, gen_charged_sent: bool = False)` with `to_dict() -> dict` and
    `State.from_dict(data: dict) -> State`.
  - `initial_state(snapshot: Snapshot) -> State`.
  - `detect(state: State | None, snapshot: Snapshot, thresholds: Thresholds) -> tuple[State, list[Event]]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_detector.py`:

```python
from lxbot.monitor.detector import (
    Audience,
    Event,
    EventType,
    State,
    Thresholds,
    detect,
)
from tests.factories import at, make_snapshot

T = Thresholds()


def run(snapshots, state=None):
    events = []
    for snapshot in snapshots:
        state, new = detect(state, snapshot, T)
        events += new
    return state, events


def polls(first, count, **kwargs):
    return [make_snapshot(first + i, **kwargs) for i in range(count)]


def types(events):
    return [event.type for event in events]


def test_first_snapshot_is_silent():
    state, events = run([make_snapshot(0)])
    assert events == []
    assert state.grid.value is True
    assert state.gen.value is False


def test_outage_is_reported_from_its_first_poll():
    _, events = run([make_snapshot(0)] + polls(1, 3, grid=False))
    assert events == [Event(EventType.GRID_LOST, at(1), 80, previous_duration=at(1) - at(0))]


def test_flapping_under_the_debounce_is_ignored():
    pattern = [True, False, False, True, False, False, True]
    _, events = run([make_snapshot(i, grid=g) for i, g in enumerate(pattern)])
    assert events == []


def test_restore_while_generator_runs_says_generator_can_stop():
    snapshots = (
        [make_snapshot(0)]
        + polls(1, 3, grid=False)
        + polls(4, 3, grid=False, gen=True)
        + polls(7, 3, grid=True, gen=True)
    )
    _, events = run(snapshots)
    assert types(events) == [EventType.GRID_LOST, EventType.GEN_STARTED, EventType.GRID_RESTORED]
    restored = events[-1]
    assert restored.at == at(7)
    assert restored.previous_duration == at(7) - at(1)
    assert restored.gen_was_running is True


def test_restore_without_generator():
    _, events = run([make_snapshot(0)] + polls(1, 3, grid=False) + polls(4, 3))
    assert events[-1].type == EventType.GRID_RESTORED
    assert events[-1].gen_was_running is False


def test_low_battery_fires_once_and_rearms():
    socs = [31, 30, 29, 35, 30]
    _, events = run([make_snapshot(i, grid=False, soc=s) for i, s in enumerate(socs)])
    assert events == [
        Event(EventType.BATTERY_LOW, at(1), 30),
        Event(EventType.BATTERY_LOW, at(4), 30),
    ]


def test_crossing_both_thresholds_sends_only_critical():
    socs = [40, 14, 13, 19]
    _, events = run([make_snapshot(i, grid=False, soc=s) for i, s in enumerate(socs)])
    assert events == [Event(EventType.BATTERY_CRITICAL, at(1), 14)]


def test_critical_rearms_at_twenty():
    socs = [16, 15, 20, 15]
    _, events = run([make_snapshot(i, grid=False, soc=s) for i, s in enumerate(socs)])
    assert types(events) == [EventType.BATTERY_LOW, EventType.BATTERY_CRITICAL, EventType.BATTERY_CRITICAL]


def test_battery_already_low_when_the_grid_goes():
    _, events = run([make_snapshot(0, soc=25)] + polls(1, 3, grid=False, soc=25))
    assert events == [
        Event(EventType.GRID_LOST, at(1), 25, previous_duration=at(1) - at(0)),
        Event(EventType.BATTERY_LOW, at(3), 25),
    ]


def test_first_start_off_grid_with_low_battery_alerts():
    _, events = run([make_snapshot(0, grid=False, soc=20)])
    assert types(events) == [EventType.BATTERY_LOW]


def test_no_low_alert_while_generator_runs():
    _, events = run(polls(0, 3, grid=False, gen=True, soc=25))
    assert events == []


def test_gen_charged_once_per_generator_run():
    snapshots = (
        [make_snapshot(0, grid=False, gen=True, soc=89)]
        + [make_snapshot(1, grid=False, gen=True, soc=90)]
        + [make_snapshot(2, grid=False, gen=True, soc=91)]
        + polls(3, 3, grid=False, gen=False, soc=91)
        + polls(6, 3, grid=False, gen=True, soc=95)
    )
    _, events = run(snapshots)
    assert types(events) == [
        EventType.GEN_CHARGED,
        EventType.GEN_STOPPED,
        EventType.GEN_STARTED,
        EventType.GEN_CHARGED,
    ]
    assert events[0] == Event(EventType.GEN_CHARGED, at(1), 90)


def test_no_gen_charged_while_grid_is_present():
    _, events = run([make_snapshot(0, grid=True, gen=True, soc=95)])
    assert events == []


def test_state_round_trips_through_a_dict():
    state, _ = run([make_snapshot(0)] + polls(1, 2, grid=False))
    assert state.grid.pending_count == 2
    assert State.from_dict(state.to_dict()) == state


def test_restart_continues_from_stored_state():
    snapshots = [make_snapshot(0)] + polls(1, 3, grid=False) + polls(4, 3)
    _, uninterrupted = run(snapshots)
    state, first_half = run(snapshots[:3])
    _, second_half = run(snapshots[3:], State.from_dict(state.to_dict()))
    assert first_half + second_half == uninterrupted


def test_audience():
    assert Event(EventType.GRID_LOST, at(0)).audience == Audience.SUBSCRIBERS
    assert Event(EventType.GEN_STARTED, at(0)).audience == Audience.JOURNAL
    assert Event(EventType.DONGLE_DOWN, at(0)).audience == Audience.ADMIN


def test_payload():
    event = Event(EventType.GRID_RESTORED, at(4), 70, previous_duration=at(4) - at(0), gen_was_running=True)
    assert event.to_payload() == {"soc": 70, "previous_duration_s": 60, "gen_was_running": True}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_detector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.monitor'`

- [ ] **Step 3: Implement `lxbot/monitor/detector.py`**

```python
"""Turns the snapshot stream into grid, generator and battery events. Pure: no I/O."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from lxbot.luxpower.registers import Snapshot


class EventType(StrEnum):
    GRID_LOST = "grid_lost"
    GRID_RESTORED = "grid_restored"
    BATTERY_LOW = "battery_low"
    BATTERY_CRITICAL = "battery_critical"
    GEN_CHARGED = "gen_charged"
    GEN_STARTED = "gen_started"
    GEN_STOPPED = "gen_stopped"
    DONGLE_DOWN = "dongle_down"
    DONGLE_UP = "dongle_up"


class Audience(StrEnum):
    SUBSCRIBERS = "subscribers"
    ADMIN = "admin"
    JOURNAL = "journal"


_AUDIENCES = {
    EventType.GEN_STARTED: Audience.JOURNAL,
    EventType.GEN_STOPPED: Audience.JOURNAL,
    EventType.DONGLE_DOWN: Audience.ADMIN,
    EventType.DONGLE_UP: Audience.ADMIN,
}


@dataclass(frozen=True)
class Event:
    type: EventType
    at: datetime
    soc: int | None = None
    previous_duration: timedelta | None = None
    gen_was_running: bool = False

    @property
    def audience(self) -> Audience:
        return _AUDIENCES.get(self.type, Audience.SUBSCRIBERS)

    def to_payload(self) -> dict:
        duration = self.previous_duration
        return {
            "soc": self.soc,
            "previous_duration_s": int(duration.total_seconds()) if duration is not None else None,
            "gen_was_running": self.gen_was_running,
        }


@dataclass(frozen=True)
class Thresholds:
    soc_low: int = 30
    soc_critical: int = 15
    soc_gen_full: int = 90
    rearm_margin: int = 5
    confirm_polls: int = 3


@dataclass(frozen=True)
class Debounced:
    value: bool
    since: datetime
    pending_since: datetime | None = None
    pending_count: int = 0

    def observe(self, observed: bool, at: datetime, confirm_polls: int) -> "Debounced":
        if observed == self.value:
            return Debounced(self.value, self.since)
        pending_since = self.pending_since or at
        pending_count = self.pending_count + 1
        if pending_count >= confirm_polls:
            return Debounced(observed, pending_since)
        return Debounced(self.value, self.since, pending_since, pending_count)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "since": self.since.isoformat(),
            "pending_since": self.pending_since.isoformat() if self.pending_since else None,
            "pending_count": self.pending_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Debounced":
        pending_since = data["pending_since"]
        return cls(
            value=data["value"],
            since=datetime.fromisoformat(data["since"]),
            pending_since=datetime.fromisoformat(pending_since) if pending_since else None,
            pending_count=data["pending_count"],
        )


@dataclass(frozen=True)
class State:
    grid: Debounced
    gen: Debounced
    low_sent: bool = False
    critical_sent: bool = False
    gen_charged_sent: bool = False

    def to_dict(self) -> dict:
        return {
            "grid": self.grid.to_dict(),
            "gen": self.gen.to_dict(),
            "low_sent": self.low_sent,
            "critical_sent": self.critical_sent,
            "gen_charged_sent": self.gen_charged_sent,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        return cls(
            grid=Debounced.from_dict(data["grid"]),
            gen=Debounced.from_dict(data["gen"]),
            low_sent=data["low_sent"],
            critical_sent=data["critical_sent"],
            gen_charged_sent=data["gen_charged_sent"],
        )


def initial_state(snapshot: Snapshot) -> State:
    return State(
        grid=Debounced(snapshot.grid_present, snapshot.taken_at),
        gen=Debounced(snapshot.gen_running, snapshot.taken_at),
    )


def detect(state: State | None, snapshot: Snapshot, thresholds: Thresholds) -> tuple[State, list[Event]]:
    if state is None:
        state = initial_state(snapshot)
    soc = snapshot.soc
    events: list[Event] = []
    grid = state.grid.observe(snapshot.grid_present, snapshot.taken_at, thresholds.confirm_polls)
    gen = state.gen.observe(snapshot.gen_running, snapshot.taken_at, thresholds.confirm_polls)
    new = replace(state, grid=grid, gen=gen)

    if grid.value != state.grid.value:
        duration = grid.since - state.grid.since
        if grid.value:
            events.append(Event(EventType.GRID_RESTORED, grid.since, soc, duration, gen_was_running=gen.value))
            new = replace(new, low_sent=False, critical_sent=False)
        else:
            events.append(Event(EventType.GRID_LOST, grid.since, soc, duration))

    if gen.value != state.gen.value:
        events.append(Event(EventType.GEN_STARTED if gen.value else EventType.GEN_STOPPED, gen.since, soc))
        if gen.value:
            new = replace(new, gen_charged_sent=False)

    if soc >= thresholds.soc_low + thresholds.rearm_margin:
        new = replace(new, low_sent=False)
    if soc >= thresholds.soc_critical + thresholds.rearm_margin:
        new = replace(new, critical_sent=False)

    if not grid.value and not gen.value:
        if soc <= thresholds.soc_critical and not new.critical_sent:
            events.append(Event(EventType.BATTERY_CRITICAL, snapshot.taken_at, soc))
            new = replace(new, critical_sent=True, low_sent=True)
        elif soc <= thresholds.soc_low and not new.low_sent:
            events.append(Event(EventType.BATTERY_LOW, snapshot.taken_at, soc))
            new = replace(new, low_sent=True)

    if gen.value and not grid.value and soc >= thresholds.soc_gen_full and not new.gen_charged_sent:
        events.append(Event(EventType.GEN_CHARGED, snapshot.taken_at, soc))
        new = replace(new, gen_charged_sent=True)

    return new, events
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_detector.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add lxbot/monitor/__init__.py lxbot/monitor/detector.py tests/test_detector.py
git commit -m "Add grid, generator and battery event detector

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: SQLite storage

**Files:**
- Create: `lxbot/storage/__init__.py` (empty), `lxbot/storage/db.py`
- Create: `tests/conftest.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces (`lxbot.storage.db`):
  - `class SubscriberStatus(StrEnum)`: `PENDING="pending"`, `APPROVED="approved"`,
    `REJECTED="rejected"`, `INACTIVE="inactive"`.
  - frozen dataclass `Subscriber(chat_id: int, name: str, username: str | None,
    status: SubscriberStatus, created_at: datetime, updated_at: datetime)`.
  - frozen dataclass `StoredEvent(at: datetime, type: str, payload: dict)`.
  - `class Database` with `async classmethod open(path: Path | str) -> Database`, `async close()`,
    `async get_subscriber(chat_id: int) -> Subscriber | None`,
    `async save_subscriber(chat_id: int, name: str, username: str | None, status: SubscriberStatus) -> Subscriber`
    (insert or update, keeps `created_at`),
    `async set_subscriber_status(chat_id: int, status: SubscriberStatus) -> None`,
    `async subscribers(*statuses: SubscriberStatus) -> list[Subscriber]` (all when no statuses; oldest first),
    `async add_event(event_type: str, at: datetime, payload: dict) -> None`,
    `async recent_events(limit: int = 100) -> list[StoredEvent]` (newest first),
    `async load_state(key: str) -> dict | None`, `async save_state(key: str, value: dict) -> None`.
- Produces (`tests/conftest.py`): fixture `db` (open `Database` on a temp file).

- [ ] **Step 1: Write the failing tests**

`tests/conftest.py`:

```python
import pytest

from lxbot.storage.db import Database


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(tmp_path / "data" / "bot.sqlite3")
    yield database
    await database.close()
```

`tests/test_db.py`:

```python
from lxbot.storage.db import Database, SubscriberStatus
from tests.factories import at


async def test_save_and_get_subscriber(db):
    saved = await db.save_subscriber(42, "Оля", "olya", SubscriberStatus.PENDING)
    assert saved.chat_id == 42
    assert saved.status is SubscriberStatus.PENDING
    assert await db.get_subscriber(42) == saved
    assert await db.get_subscriber(7) is None


async def test_save_subscriber_updates_and_keeps_created_at(db):
    first = await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    second = await db.save_subscriber(42, "Ольга", "olga", SubscriberStatus.APPROVED)
    assert second.created_at == first.created_at
    assert second.name == "Ольга"
    assert second.status is SubscriberStatus.APPROVED


async def test_filter_subscribers_by_status(db):
    await db.save_subscriber(1, "A", None, SubscriberStatus.APPROVED)
    await db.save_subscriber(2, "B", None, SubscriberStatus.PENDING)
    await db.save_subscriber(3, "C", None, SubscriberStatus.REJECTED)
    await db.set_subscriber_status(2, SubscriberStatus.APPROVED)
    approved = await db.subscribers(SubscriberStatus.APPROVED)
    assert [s.chat_id for s in approved] == [1, 2]
    assert len(await db.subscribers()) == 3


async def test_events_journal(db):
    await db.add_event("grid_lost", at(1), {"soc": 80})
    await db.add_event("grid_restored", at(5), {"soc": 70})
    events = await db.recent_events()
    assert [e.type for e in events] == ["grid_restored", "grid_lost"]
    assert events[1].at == at(1)
    assert events[1].payload == {"soc": 80}


async def test_state_round_trip(db):
    assert await db.load_state("detector") is None
    await db.save_state("detector", {"a": 1})
    await db.save_state("detector", {"a": 2})
    assert await db.load_state("detector") == {"a": 2}


async def test_data_survives_reopen(tmp_path):
    path = tmp_path / "bot.sqlite3"
    first = await Database.open(path)
    await first.save_state("detector", {"a": 1})
    await first.close()
    second = await Database.open(path)
    assert await second.load_state("detector") == {"a": 1}
    await second.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.storage'`

- [ ] **Step 3: Implement `lxbot/storage/db.py`**

```python
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SubscriberStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class Subscriber:
    chat_id: int
    name: str
    username: str | None
    status: SubscriberStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoredEvent:
    at: datetime
    type: str
    payload: dict


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _subscriber(row: aiosqlite.Row) -> Subscriber:
    return Subscriber(
        chat_id=row["chat_id"],
        name=row["name"],
        username=row["username"],
        status=SubscriberStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> "Database":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def get_subscriber(self, chat_id: int) -> Subscriber | None:
        async with self._conn.execute("SELECT * FROM subscribers WHERE chat_id = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
        return _subscriber(row) if row else None

    async def save_subscriber(
        self, chat_id: int, name: str, username: str | None, status: SubscriberStatus
    ) -> Subscriber:
        now = _now()
        await self._conn.execute(
            """
            INSERT INTO subscribers (chat_id, name, username, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (chat_id, name, username, status.value, now, now),
        )
        await self._conn.commit()
        return await self.get_subscriber(chat_id)

    async def set_subscriber_status(self, chat_id: int, status: SubscriberStatus) -> None:
        await self._conn.execute(
            "UPDATE subscribers SET status = ?, updated_at = ? WHERE chat_id = ?",
            (status.value, _now(), chat_id),
        )
        await self._conn.commit()

    async def subscribers(self, *statuses: SubscriberStatus) -> list[Subscriber]:
        query = "SELECT * FROM subscribers"
        params: tuple = ()
        if statuses:
            query += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            params = tuple(s.value for s in statuses)
        async with self._conn.execute(query + " ORDER BY created_at, chat_id", params) as cursor:
            return [_subscriber(row) for row in await cursor.fetchall()]

    async def add_event(self, event_type: str, at: datetime, payload: dict) -> None:
        await self._conn.execute(
            "INSERT INTO events (at, type, payload) VALUES (?, ?, ?)",
            (at.isoformat(), str(event_type), json.dumps(payload)),
        )
        await self._conn.commit()

    async def recent_events(self, limit: int = 100) -> list[StoredEvent]:
        async with self._conn.execute(
            "SELECT at, type, payload FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [StoredEvent(datetime.fromisoformat(r["at"]), r["type"], json.loads(r["payload"])) for r in rows]

    async def load_state(self, key: str) -> dict | None:
        async with self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["value"]) if row else None

    async def save_state(self, key: str, value: dict) -> None:
        await self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        await self._conn.commit()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add lxbot/storage/__init__.py lxbot/storage/db.py tests/conftest.py tests/test_db.py
git commit -m "Add SQLite storage for subscribers, events and state

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Ukrainian texts

**Files:**
- Create: `lxbot/bot/__init__.py` (empty), `lxbot/bot/texts.py`
- Test: `tests/test_texts.py`

**Interfaces:**
- Consumes: `Event`, `EventType`, `State`, `Debounced` (Task 4); `Snapshot` (Task 2);
  `Subscriber`, `SubscriberStatus` (Task 5).
- Produces (`lxbot.bot.texts`): constants `STATUS_BUTTON`, `REQUEST_SENT`, `REQUEST_PENDING`,
  `REQUEST_REJECTED`, `SUBSCRIBED`, `ALREADY_SUBSCRIBED`, `UNSUBSCRIBED`, `NOT_SUBSCRIBED`, `NO_DATA`,
  `NO_SUBSCRIBERS`, `APPROVED_MARK`, `REJECTED_MARK`, `ALREADY_DECIDED`, `NOT_FOUND`;
  functions `format_duration(duration: timedelta) -> str`, `format_power(watts: int) -> str`,
  `event_text(event: Event, tz: tzinfo) -> str | None`,
  `status_text(snapshot: Snapshot | None, state: State | None, now: datetime, tz: tzinfo) -> str`,
  `admin_request_text(name: str, username: str | None, chat_id: int) -> str`,
  `subscribers_text(subscribers: list[Subscriber]) -> str`, `removed_text(name: str) -> str`.
  All texts are Telegram HTML.

- [ ] **Step 1: Write the failing tests**

`tests/test_texts.py`:

```python
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from lxbot.bot import texts
from lxbot.monitor.detector import Debounced, Event, EventType, State
from tests.factories import at, make_snapshot

KYIV = ZoneInfo("Europe/Kyiv")


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (timedelta(seconds=30), "менше хвилини"),
        (timedelta(minutes=45), "45 хв"),
        (timedelta(hours=3), "3 год"),
        (timedelta(hours=16, minutes=52), "16 год 52 хв"),
        (timedelta(days=2, hours=3, minutes=5), "2 дн 3 год"),
    ],
)
def test_format_duration(duration, expected):
    assert texts.format_duration(duration) == expected


@pytest.mark.parametrize(("watts", "expected"), [(850, "850 Вт"), (1200, "1.2 кВт"), (-1200, "1.2 кВт")])
def test_format_power(watts, expected):
    assert texts.format_power(watts) == expected


def test_grid_lost_text_uses_kyiv_time():
    event = Event(EventType.GRID_LOST, at(72), 87, previous_duration=timedelta(hours=16, minutes=52))
    text = texts.event_text(event, KYIV)
    assert "10:18 Світло зникло" in text  # 07:18 UTC is 10:18 EEST
    assert "16 год 52 хв" in text
    assert "87%" in text


def test_grid_restored_mentions_generator_only_when_it_ran():
    with_gen = Event(EventType.GRID_RESTORED, at(0), 60, timedelta(hours=1), gen_was_running=True)
    without = Event(EventType.GRID_RESTORED, at(0), 60, timedelta(hours=1))
    assert "Генератор можна вимикати" in texts.event_text(with_gen, KYIV)
    assert "Генератор" not in texts.event_text(without, KYIV)


@pytest.mark.parametrize(
    ("event_type", "fragment"),
    [
        (EventType.BATTERY_LOW, "Час заводити генератор"),
        (EventType.BATTERY_CRITICAL, "Терміново заведіть генератор"),
        (EventType.GEN_CHARGED, "Генератор можна вимикати"),
        (EventType.DONGLE_DOWN, "Немає зв'язку з інвертором"),
        (EventType.DONGLE_UP, "Зв'язок з інвертором відновлено"),
    ],
)
def test_other_event_texts(event_type, fragment):
    assert fragment in texts.event_text(Event(event_type, at(0), 30), KYIV)


def test_journal_only_events_have_no_text():
    assert texts.event_text(Event(EventType.GEN_STARTED, at(0)), KYIV) is None


def test_status_text_on_grid():
    snapshot = make_snapshot(0, soc=76, battery_w=1200, load_w=850, pv_w=0, grid_v=229.4)
    state = State(grid=Debounced(True, at(0)), gen=Debounced(False, at(0)))
    text = texts.status_text(snapshot, state, at(1), KYIV)
    assert "⚡ Мережа: є · 229 В" in text
    assert "⛽ Генератор: не працює" in text
    assert "🔋 Батарея: 76% · заряджається 1.2 кВт" in text
    assert "🏠 Споживання: 850 Вт" in text
    assert "Оновлено 10:00:00" in text
    assert "застарілі" not in text


def test_status_text_off_grid_shows_outage_length():
    snapshot = make_snapshot(540, grid=False, battery_w=-500)
    state = State(grid=Debounced(False, at(0)), gen=Debounced(False, at(0)))
    text = texts.status_text(snapshot, state, at(540), KYIV)
    assert "немає вже 2 год 15 хв" in text
    assert "розряджається 500 Вт" in text


def test_status_text_marks_stale_data():
    state = State(grid=Debounced(True, at(0)), gen=Debounced(False, at(0)))
    text = texts.status_text(make_snapshot(0), state, at(0) + timedelta(minutes=3), KYIV)
    assert "застарілі" in text


def test_status_text_without_data():
    assert texts.status_text(None, None, at(0), KYIV) == texts.NO_DATA


def test_admin_request_text_escapes_html():
    text = texts.admin_request_text("Оля <3 & Co", "olya", 42)
    assert "Оля &lt;3 &amp; Co" in text
    assert "@olya" in text
    assert "42" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_texts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.bot'`

- [ ] **Step 3: Implement `lxbot/bot/texts.py`**

```python
from datetime import datetime, timedelta, tzinfo
from html import escape

from lxbot.luxpower.registers import Snapshot
from lxbot.monitor.detector import Event, EventType, State
from lxbot.storage.db import Subscriber, SubscriberStatus

STALE_AFTER = timedelta(minutes=2)

STATUS_BUTTON = "📊 Статус"
REQUEST_SENT = "📨 Запит надіслано адміністратору. Я напишу, щойно його схвалять."
REQUEST_PENDING = "⏳ Запит ще розглядається."
REQUEST_REJECTED = "🚫 Доступ не надано."
SUBSCRIBED = "✅ Підписку підтверджено. Я повідомлятиму про світло, генератор і заряд батареї."
ALREADY_SUBSCRIBED = "✅ Ви вже підписані."
UNSUBSCRIBED = "👋 Підписку скасовано. Щоб повернутися, натисніть /start."
NOT_SUBSCRIBED = "Натисніть /start, щоб надіслати запит на доступ."
NO_DATA = "⏳ Ще немає даних від інвертора."
NO_SUBSCRIBERS = "Підписників немає."
APPROVED_MARK = "✅ Схвалено"
REJECTED_MARK = "❌ Відхилено"
ALREADY_DECIDED = "Запит уже оброблено"
NOT_FOUND = "Не знайдено"

_STATUS_ICONS = {SubscriberStatus.APPROVED: "✅", SubscriberStatus.PENDING: "⏳"}


def format_duration(duration: timedelta) -> str:
    minutes = int(duration.total_seconds() // 60)
    if minutes < 1:
        return "менше хвилини"
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} год")
    if mins and not days:
        parts.append(f"{mins} хв")
    return " ".join(parts)


def format_power(watts: int) -> str:
    watts = abs(watts)
    return f"{watts} Вт" if watts < 1000 else f"{watts / 1000:.1f} кВт"


def _clock(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def event_text(event: Event, tz: tzinfo) -> str | None:
    time = _clock(event.at, tz)
    match event.type:
        case EventType.GRID_LOST:
            return (
                f"🔴 <b>{time} Світло зникло</b>\n"
                f"🕓 Воно було {format_duration(event.previous_duration)}\n"
                f"🔋 Заряд {event.soc}%"
            )
        case EventType.GRID_RESTORED:
            text = f"🟢 <b>{time} Світло з'явилось</b>\n🕓 Його не було {format_duration(event.previous_duration)}"
            if event.gen_was_running:
                text += "\n⛽ Генератор можна вимикати"
            return text
        case EventType.BATTERY_LOW:
            return f"🪫 <b>Залишилось {event.soc}%</b>\n⛽ Час заводити генератор"
        case EventType.BATTERY_CRITICAL:
            return f"🚨 <b>Залишилось {event.soc}%</b>\n⛽ Терміново заведіть генератор"
        case EventType.GEN_CHARGED:
            return f"🔋 <b>Заряджено {event.soc}%</b>\n⛽ Генератор можна вимикати"
        case EventType.DONGLE_DOWN:
            return f"⚠️ Немає зв'язку з інвертором з {time}"
        case EventType.DONGLE_UP:
            return f"✅ Зв'язок з інвертором відновлено о {time}"
        case _:
            return None


def status_text(snapshot: Snapshot | None, state: State | None, now: datetime, tz: tzinfo) -> str:
    if snapshot is None or state is None:
        return NO_DATA
    if state.grid.value:
        grid = f"є · {snapshot.grid_v:.0f} В"
    else:
        grid = f"немає вже {format_duration(now - state.grid.since)}"
    if snapshot.battery_w > 0:
        flow = f" · заряджається {format_power(snapshot.battery_w)}"
    elif snapshot.battery_w < 0:
        flow = f" · розряджається {format_power(snapshot.battery_w)}"
    else:
        flow = ""
    lines = [
        f"⚡ Мережа: {grid}",
        f"⛽ Генератор: {'працює' if state.gen.value else 'не працює'}",
        f"🔋 Батарея: {snapshot.soc}%{flow}",
        f"🏠 Споживання: {format_power(snapshot.load_w)}",
        f"☀️ Сонце: {format_power(snapshot.pv_w)}",
        f"🕐 Оновлено {snapshot.taken_at.astimezone(tz):%H:%M:%S}",
    ]
    if now - snapshot.taken_at > STALE_AFTER:
        lines.append("⚠️ Дані застарілі: немає зв'язку з інвертором")
    return "\n".join(lines)


def _who(name: str, username: str | None) -> str:
    return escape(name) + (f" @{escape(username)}" if username else "")


def admin_request_text(name: str, username: str | None, chat_id: int) -> str:
    return f"🆕 Новий запит на доступ\n{_who(name, username)}\nID: {chat_id}"


def subscribers_text(subscribers: list[Subscriber]) -> str:
    lines = ["👥 Підписники:"]
    lines += [f"{_STATUS_ICONS.get(s.status, '•')} {_who(s.name, s.username)}" for s in subscribers]
    return "\n".join(lines)


def removed_text(name: str) -> str:
    return f"Видалено: {name}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_texts.py -v`
Expected: 21 passed

- [ ] **Step 5: Commit**

```bash
git add lxbot/bot/__init__.py lxbot/bot/texts.py tests/test_texts.py
git commit -m "Add Ukrainian notification and status texts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Notifier

**Files:**
- Create: `lxbot/notify/__init__.py` (empty), `lxbot/notify/broadcast.py`
- Test: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `Event`, `Audience` (Task 4); `Database`, `SubscriberStatus` (Task 5);
  `event_text` (Task 6).
- Produces (`lxbot.notify.broadcast`): `class Sender(Protocol)` with
  `async send_message(chat_id: int, text: str, **kwargs) -> object` (aiogram's `Bot` satisfies it);
  `class Notifier(sender: Sender, db: Database, admin_chat_id: int, tz: tzinfo, *, retry_base: float = 1.0)`
  with `async publish(events: Sequence[Event]) -> None` (journals every event, queues texts, never
  waits for Telegram), `async run() -> None` (delivery worker, runs forever),
  `async join() -> None` (waits until the queue is delivered; used by tests).

- [ ] **Step 1: Write the failing tests**

`tests/test_notifier.py`:

```python
import asyncio
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage

from lxbot.monitor.detector import Event, EventType
from lxbot.notify.broadcast import Notifier
from lxbot.storage.db import SubscriberStatus
from tests.factories import at

ADMIN = 1
METHOD = SendMessage(chat_id=0, text="x")


class FakeSender:
    def __init__(self, failures: dict[int, list[Exception]] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.failures = failures or {}

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if self.failures.get(chat_id):
            raise self.failures[chat_id].pop(0)
        self.sent.append((chat_id, text))


@pytest.fixture
async def subscribers(db):
    await db.save_subscriber(10, "A", None, SubscriberStatus.APPROVED)
    await db.save_subscriber(11, "B", None, SubscriberStatus.APPROVED)
    await db.save_subscriber(12, "C", None, SubscriberStatus.PENDING)


async def deliver(notifier: Notifier, events) -> None:
    worker = asyncio.create_task(notifier.run())
    await notifier.publish(events)
    await asyncio.wait_for(notifier.join(), 2)
    worker.cancel()


def make_notifier(sender, db) -> Notifier:
    return Notifier(sender, db, ADMIN, ZoneInfo("Europe/Kyiv"), retry_base=0)


async def test_subscriber_events_go_to_approved_subscribers(db, subscribers):
    sender = FakeSender()
    await deliver(make_notifier(sender, db), [Event(EventType.BATTERY_LOW, at(0), 30)])
    assert [chat_id for chat_id, _ in sender.sent] == [10, 11]
    assert [e.type for e in await db.recent_events()] == ["battery_low"]


async def test_admin_events_go_to_admin_only(db, subscribers):
    sender = FakeSender()
    await deliver(make_notifier(sender, db), [Event(EventType.DONGLE_DOWN, at(0))])
    assert [chat_id for chat_id, _ in sender.sent] == [ADMIN]


async def test_journal_only_events_are_not_sent(db, subscribers):
    sender = FakeSender()
    await deliver(make_notifier(sender, db), [Event(EventType.GEN_STARTED, at(0), 50)])
    assert sender.sent == []
    assert [e.type for e in await db.recent_events()] == ["gen_started"]


async def test_network_errors_are_retried(db, subscribers):
    errors = [TelegramNetworkError(METHOD, "timeout"), TelegramNetworkError(METHOD, "timeout")]
    sender = FakeSender({10: errors})
    await deliver(make_notifier(sender, db), [Event(EventType.BATTERY_LOW, at(0), 30)])
    assert [chat_id for chat_id, _ in sender.sent] == [10, 11]


async def test_blocked_user_becomes_inactive(db, subscribers):
    sender = FakeSender({10: [TelegramForbiddenError(METHOD, "Forbidden: bot was blocked by the user")]})
    await deliver(make_notifier(sender, db), [Event(EventType.BATTERY_LOW, at(0), 30)])
    assert (await db.get_subscriber(10)).status is SubscriberStatus.INACTIVE
    assert [chat_id for chat_id, _ in sender.sent] == [11]


async def test_publish_does_not_wait_for_delivery(db, subscribers):
    class HangingSender:
        async def send_message(self, chat_id: int, text: str, **kwargs):
            await asyncio.Event().wait()

    notifier = make_notifier(HangingSender(), db)
    worker = asyncio.create_task(notifier.run())
    await asyncio.wait_for(notifier.publish([Event(EventType.GRID_LOST, at(1), 80, at(1) - at(0))]), 1)
    worker.cancel()
    assert [e.type for e in await db.recent_events()] == ["grid_lost"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_notifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.notify'`

- [ ] **Step 3: Implement `lxbot/notify/broadcast.py`**

```python
import asyncio
import logging
from collections.abc import Sequence
from datetime import tzinfo
from typing import Protocol

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from lxbot.bot.texts import event_text
from lxbot.monitor.detector import Audience, Event
from lxbot.storage.db import Database, SubscriberStatus

log = logging.getLogger(__name__)

_RETRY_MAX = 60.0


class Sender(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs) -> object: ...


class Notifier:
    def __init__(self, sender: Sender, db: Database, admin_chat_id: int, tz: tzinfo, *, retry_base: float = 1.0):
        self._sender = sender
        self._db = db
        self._admin_chat_id = admin_chat_id
        self._tz = tz
        self._retry_base = retry_base
        self._queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()

    async def publish(self, events: Sequence[Event]) -> None:
        for event in events:
            await self._db.add_event(event.type, event.at, event.to_payload())
            text = event_text(event, self._tz)
            if text is None:
                continue
            for chat_id in await self._recipients(event.audience):
                self._queue.put_nowait((chat_id, text))

    async def run(self) -> None:
        while True:
            chat_id, text = await self._queue.get()
            try:
                await self._deliver(chat_id, text)
            except Exception:
                log.exception("dropping a message to %s", chat_id)
            finally:
                self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    async def _recipients(self, audience: Audience) -> list[int]:
        if audience is Audience.ADMIN:
            return [self._admin_chat_id]
        return [s.chat_id for s in await self._db.subscribers(SubscriberStatus.APPROVED)]

    async def _deliver(self, chat_id: int, text: str) -> None:
        # Keep retrying network failures: the text carries the event time, so late is still useful.
        delay = self._retry_base
        while True:
            try:
                await self._sender.send_message(chat_id=chat_id, text=text)
                return
            except TelegramForbiddenError:
                log.info("chat %s blocked the bot; marking inactive", chat_id)
                await self._db.set_subscriber_status(chat_id, SubscriberStatus.INACTIVE)
                return
            except TelegramBadRequest as exc:
                log.error("Telegram rejected a message to %s: %s", chat_id, exc)
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except (TelegramNetworkError, TelegramServerError) as exc:
                log.warning("sending to %s failed, retrying in %.0f s: %s", chat_id, delay, exc)
                await asyncio.sleep(delay)
                delay = min(max(delay * 2, self._retry_base), _RETRY_MAX)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_notifier.py -v`
Expected: 6 passed. If constructing an aiogram exception fails, check its signature in
`.venv/lib/python3.12/site-packages/aiogram/exceptions.py` and adjust the test's constructor call
only.

- [ ] **Step 5: Commit**

```bash
git add lxbot/notify/__init__.py lxbot/notify/broadcast.py tests/test_notifier.py
git commit -m "Add notifier with event journal and queued delivery

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Poller and dongle watchdog

**Files:**
- Create: `lxbot/monitor/watchdog.py`, `lxbot/monitor/poller.py`
- Test: `tests/test_watchdog.py`, `tests/test_poller.py`

**Interfaces:**
- Consumes: `DongleError` (Task 3); `Snapshot` (Task 2); `Event`, `EventType`, `State`,
  `Thresholds`, `detect` (Task 4); `Database` (Task 5); the notifier's
  `publish(events)` (Task 7).
- Produces (`lxbot.monitor.watchdog`): `DONGLE_DOWN_AFTER = timedelta(minutes=5)`;
  `class DongleWatch(down_after: timedelta = DONGLE_DOWN_AFTER)` with
  `success(now: datetime) -> list[Event]` and `failure(now: datetime) -> list[Event]`.
- Produces (`lxbot.monitor.poller`): `STATE_KEY = "detector"`;
  `class Poller(source, db: Database, sink, thresholds: Thresholds, interval: float, clock: Callable[[], datetime] = <utc now>)`
  where `source` has `async read_snapshot(taken_at: datetime) -> Snapshot` and `sink` has
  `async publish(events: Sequence[Event]) -> None`; attributes `latest: Snapshot | None`,
  `state: State | None`; methods `async load()`, `async poll_once()`, `async run()`.

- [ ] **Step 1: Write the failing tests**

`tests/test_watchdog.py`:

```python
from datetime import timedelta

from lxbot.monitor.detector import Event, EventType
from lxbot.monitor.watchdog import DongleWatch
from tests.factories import BASE


def test_down_after_five_minutes_then_up():
    watch = DongleWatch()
    assert watch.failure(BASE) == []
    assert watch.failure(BASE + timedelta(minutes=4, seconds=59)) == []
    assert watch.failure(BASE + timedelta(minutes=5)) == [Event(EventType.DONGLE_DOWN, BASE)]
    assert watch.failure(BASE + timedelta(minutes=6)) == []
    up = BASE + timedelta(minutes=7)
    assert watch.success(up) == [Event(EventType.DONGLE_UP, up)]
    assert watch.success(up) == []


def test_short_failure_is_silent():
    watch = DongleWatch()
    watch.failure(BASE)
    assert watch.success(BASE + timedelta(minutes=1)) == []
    assert watch.failure(BASE + timedelta(minutes=2)) == []
    assert watch.failure(BASE + timedelta(minutes=6)) == []
```

`tests/test_poller.py`:

```python
from datetime import timedelta

from lxbot.luxpower.client import DongleError
from lxbot.monitor.detector import EventType, Thresholds
from lxbot.monitor.poller import STATE_KEY, Poller
from tests.factories import at, make_snapshot


class ScriptedSource:
    def __init__(self, script):
        self.script = list(script)

    async def read_snapshot(self, taken_at):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingSink:
    def __init__(self):
        self.events = []

    async def publish(self, events):
        self.events.extend(events)


class Clock:
    def __init__(self, times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0)


def make_poller(script, db, sink, times=None):
    times = times if times is not None else [s.taken_at for s in script]
    return Poller(ScriptedSource(script), db, sink, Thresholds(), interval=0, clock=Clock(times))


async def poll_all(poller, count):
    await poller.load()
    for _ in range(count):
        await poller.poll_once()


async def test_outage_is_published_and_state_saved(db):
    script = [make_snapshot(0)] + [make_snapshot(i, grid=False) for i in (1, 2, 3)]
    sink = RecordingSink()
    poller = make_poller(script, db, sink)
    await poll_all(poller, 4)
    assert [e.type for e in sink.events] == [EventType.GRID_LOST]
    assert poller.latest.taken_at == at(3)
    assert (await db.load_state(STATE_KEY))["grid"]["value"] is False


async def test_restart_mid_outage_keeps_outage_start(db):
    first = [make_snapshot(0)] + [make_snapshot(i, grid=False) for i in (1, 2, 3)]
    sink = RecordingSink()
    await poll_all(make_poller(first, db, sink), 4)

    second = [make_snapshot(i, grid=False) for i in (40, 41)] + [make_snapshot(i) for i in (42, 43, 44)]
    await poll_all(make_poller(second, db, sink), 5)

    assert [e.type for e in sink.events] == [EventType.GRID_LOST, EventType.GRID_RESTORED]
    assert sink.events[1].previous_duration == at(42) - at(1)


async def test_dongle_down_and_up_reach_the_sink(db):
    failures = [DongleError("timeout")] * 3
    times = [at(0), at(0) + timedelta(minutes=3), at(0) + timedelta(minutes=5), at(30)]
    sink = RecordingSink()
    poller = make_poller(failures + [make_snapshot(30)], db, sink, times)
    await poll_all(poller, 4)
    assert [e.type for e in sink.events] == [EventType.DONGLE_DOWN, EventType.DONGLE_UP]
    assert sink.events[0].at == at(0)


async def test_state_is_written_only_when_it_changes(db, monkeypatch):
    writes = []
    save_state = db.save_state

    async def counting_save_state(key, value):
        writes.append(value)
        await save_state(key, value)

    monkeypatch.setattr(db, "save_state", counting_save_state)
    script = [make_snapshot(i) for i in range(5)]
    await poll_all(make_poller(script, db, RecordingSink()), 5)
    assert len(writes) == 1


async def test_run_survives_an_unexpected_error(db):
    class BrokenOnceSource:
        calls = 0

        async def read_snapshot(self, taken_at):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bug")
            if self.calls == 3:
                raise SystemExit  # stops run() for the test
            return make_snapshot(0)

    source = BrokenOnceSource()
    poller = Poller(source, db, RecordingSink(), Thresholds(), interval=0)
    try:
        await poller.run()
    except SystemExit:
        pass
    assert source.calls == 3
    assert poller.latest is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_watchdog.py tests/test_poller.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.monitor.watchdog'`

- [ ] **Step 3: Implement `lxbot/monitor/watchdog.py`**

```python
from datetime import datetime, timedelta

from lxbot.monitor.detector import Event, EventType

DONGLE_DOWN_AFTER = timedelta(minutes=5)


class DongleWatch:
    def __init__(self, down_after: timedelta = DONGLE_DOWN_AFTER):
        self._down_after = down_after
        self._failing_since: datetime | None = None
        self._down_reported = False

    def success(self, now: datetime) -> list[Event]:
        self._failing_since = None
        if not self._down_reported:
            return []
        self._down_reported = False
        return [Event(EventType.DONGLE_UP, now)]

    def failure(self, now: datetime) -> list[Event]:
        if self._failing_since is None:
            self._failing_since = now
        if self._down_reported or now - self._failing_since < self._down_after:
            return []
        self._down_reported = True
        return [Event(EventType.DONGLE_DOWN, self._failing_since)]
```

- [ ] **Step 4: Implement `lxbot/monitor/poller.py`**

```python
import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from lxbot.luxpower.client import DongleError
from lxbot.luxpower.registers import Snapshot
from lxbot.monitor.detector import Event, State, Thresholds, detect
from lxbot.monitor.watchdog import DongleWatch
from lxbot.storage.db import Database

log = logging.getLogger(__name__)

STATE_KEY = "detector"


class SnapshotSource(Protocol):
    async def read_snapshot(self, taken_at: datetime) -> Snapshot: ...


class EventSink(Protocol):
    async def publish(self, events: Sequence[Event]) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Poller:
    def __init__(
        self,
        source: SnapshotSource,
        db: Database,
        sink: EventSink,
        thresholds: Thresholds,
        interval: float,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._source = source
        self._db = db
        self._sink = sink
        self._thresholds = thresholds
        self._interval = interval
        self._clock = clock
        self._watch = DongleWatch()
        self._saved_state: dict | None = None
        self.latest: Snapshot | None = None
        self.state: State | None = None

    async def load(self) -> None:
        self._saved_state = await self._db.load_state(STATE_KEY)
        self.state = State.from_dict(self._saved_state) if self._saved_state else None

    async def poll_once(self) -> None:
        now = self._clock()
        try:
            snapshot = await self._source.read_snapshot(now)
        except DongleError as exc:
            log.warning("poll failed: %s", exc)
            await self._sink.publish(self._watch.failure(now))
            return
        events = self._watch.success(now)
        self.latest = snapshot
        self.state, detected = detect(self.state, snapshot, self._thresholds)
        await self._persist_state()
        await self._sink.publish(events + detected)

    async def run(self) -> None:
        await self.load()
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await self.poll_once()
            except Exception:
                log.exception("unexpected error while polling")
            await asyncio.sleep(max(0.0, self._interval - (loop.time() - started)))

    async def _persist_state(self) -> None:
        # The database sits on the board's microSD card: write only when something changed.
        data = self.state.to_dict()
        if data != self._saved_state:
            await self._db.save_state(STATE_KEY, data)
            self._saved_state = data
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_watchdog.py tests/test_poller.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add lxbot/monitor/watchdog.py lxbot/monitor/poller.py tests/test_watchdog.py tests/test_poller.py
git commit -m "Add poller with persisted state and dongle watchdog

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Configuration

**Files:**
- Create: `lxbot/config.py`
- Modify: `tests/conftest.py` (add `make_settings`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `DongleConfig` (Task 3); `Thresholds` (Task 4).
- Produces (`lxbot.config`): `class ConfigError(Exception)`; frozen dataclass
  `Settings(bot_token: str, admin_chat_id: int, dongle: DongleConfig, thresholds: Thresholds,
  poll_interval: float, tz: ZoneInfo, db_path: Path)`;
  `dongle_config_from_env(env: Mapping[str, str]) -> DongleConfig`;
  `settings_from_env(env: Mapping[str, str] = os.environ) -> Settings`.
- Produces (`tests/conftest.py`): `make_settings(admin_chat_id: int = 1) -> Settings`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/conftest.py`:

```python
from pathlib import Path
from zoneinfo import ZoneInfo

from lxbot.config import Settings
from lxbot.luxpower.client import DongleConfig
from lxbot.monitor.detector import Thresholds
from tests.fixtures import DONGLE_SERIAL, INVERTER_SERIAL


def make_settings(admin_chat_id: int = 1) -> Settings:
    return Settings(
        bot_token="test-token",
        admin_chat_id=admin_chat_id,
        dongle=DongleConfig("127.0.0.1", 8000, DONGLE_SERIAL, INVERTER_SERIAL),
        thresholds=Thresholds(),
        poll_interval=15,
        tz=ZoneInfo("Europe/Kyiv"),
        db_path=Path("unused.sqlite3"),
    )
```

`tests/test_config.py`:

```python
from pathlib import Path

import pytest

from lxbot.config import ConfigError, settings_from_env
from tests.fixtures import DONGLE_SERIAL, INVERTER_SERIAL

ENV = {
    "BOT_TOKEN": "token",
    "ADMIN_CHAT_ID": "12345",
    "DONGLE_HOST": "inverter.lan",
    "DONGLE_SERIAL": DONGLE_SERIAL,
    "INVERTER_SERIAL": INVERTER_SERIAL,
}


def test_defaults():
    settings = settings_from_env(ENV)
    assert settings.admin_chat_id == 12345
    assert settings.dongle.host == "inverter.lan"
    assert settings.dongle.port == 8000
    assert settings.poll_interval == 15
    assert (settings.thresholds.soc_low, settings.thresholds.soc_critical, settings.thresholds.soc_gen_full) == (30, 15, 90)
    assert settings.tz.key == "Europe/Kyiv"
    assert settings.db_path == Path("data/bot.sqlite3")


def test_overrides():
    env = ENV | {"DONGLE_PORT": "9000", "POLL_INTERVAL": "10", "SOC_LOW": "35", "TZ": "UTC", "DB_PATH": "/app/data/x.db"}
    settings = settings_from_env(env)
    assert settings.dongle.port == 9000
    assert settings.poll_interval == 10
    assert settings.thresholds.soc_low == 35
    assert settings.tz.key == "UTC"
    assert settings.db_path == Path("/app/data/x.db")


def test_missing_variables_are_listed():
    with pytest.raises(ConfigError, match="BOT_TOKEN, DONGLE_SERIAL"):
        settings_from_env({k: v for k, v in ENV.items() if k not in {"BOT_TOKEN", "DONGLE_SERIAL"}})


def test_bad_serial():
    with pytest.raises(ConfigError, match="DONGLE_SERIAL"):
        settings_from_env(ENV | {"DONGLE_SERIAL": "DG1"})


def test_thresholds_must_be_ordered():
    with pytest.raises(ConfigError, match="SOC_CRITICAL < SOC_LOW < SOC_GEN_FULL"):
        settings_from_env(ENV | {"SOC_CRITICAL": "40"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.config'`

- [ ] **Step 3: Implement `lxbot/config.py`**

```python
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from lxbot.luxpower.client import DongleConfig
from lxbot.luxpower.frame import SERIAL_LEN
from lxbot.monitor.detector import Thresholds

_REQUIRED = ("BOT_TOKEN", "ADMIN_CHAT_ID", "DONGLE_HOST", "DONGLE_SERIAL", "INVERTER_SERIAL")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_chat_id: int
    dongle: DongleConfig
    thresholds: Thresholds
    poll_interval: float
    tz: ZoneInfo
    db_path: Path


def _serial(env: Mapping[str, str], name: str) -> str:
    value = env[name]
    if len(value) != SERIAL_LEN or not value.isascii():
        raise ConfigError(f"{name} must be {SERIAL_LEN} ASCII characters")
    return value


def dongle_config_from_env(env: Mapping[str, str]) -> DongleConfig:
    return DongleConfig(
        host=env["DONGLE_HOST"],
        port=int(env.get("DONGLE_PORT", "8000")),
        dongle_serial=_serial(env, "DONGLE_SERIAL"),
        inverter_serial=_serial(env, "INVERTER_SERIAL"),
    )


def settings_from_env(env: Mapping[str, str] = os.environ) -> Settings:
    missing = [name for name in _REQUIRED if not env.get(name)]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    thresholds = Thresholds(
        soc_low=int(env.get("SOC_LOW", "30")),
        soc_critical=int(env.get("SOC_CRITICAL", "15")),
        soc_gen_full=int(env.get("SOC_GEN_FULL", "90")),
    )
    if not thresholds.soc_critical < thresholds.soc_low < thresholds.soc_gen_full:
        raise ConfigError("thresholds must satisfy SOC_CRITICAL < SOC_LOW < SOC_GEN_FULL")
    return Settings(
        bot_token=env["BOT_TOKEN"],
        admin_chat_id=int(env["ADMIN_CHAT_ID"]),
        dongle=dongle_config_from_env(env),
        thresholds=thresholds,
        poll_interval=float(env.get("POLL_INTERVAL", "15")),
        tz=ZoneInfo(env.get("TZ", "Europe/Kyiv")),
        db_path=Path(env.get("DB_PATH", "data/bot.sqlite3")),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add lxbot/config.py tests/conftest.py tests/test_config.py
git commit -m "Add settings from environment

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Subscriptions and bot handlers

**Files:**
- Create: `lxbot/bot/subscriptions.py`, `lxbot/bot/handlers.py`
- Test: `tests/test_subscriptions.py`, `tests/test_handlers.py`

**Interfaces:**
- Consumes: `Database`, `Subscriber`, `SubscriberStatus` (Task 5); `texts` (Task 6);
  `Settings` (Task 9); a poller-like object with `latest` and `state` (Task 8);
  `make_settings`, `db` fixture (conftest); `make_snapshot` (factories); `initial_state` (Task 4).
- Produces (`lxbot.bot.subscriptions`): `class StartOutcome(StrEnum)`: `SUBSCRIBED`, `ALREADY`,
  `PENDING`, `REQUESTED`, `REJECTED`;
  `async start(db, chat_id: int, name: str, username: str | None, admin_chat_id: int) -> StartOutcome`;
  `async decide(db, chat_id: int, approve: bool) -> Subscriber | None` (acts only on a pending
  request); `async stop(db, chat_id: int) -> None`; `async remove(db, chat_id: int) -> Subscriber | None`;
  `async is_approved(db, chat_id: int) -> bool`.
- Produces (`lxbot.bot.handlers`): `router: Router`; handlers `on_start`, `on_decision`,
  `on_status`, `on_stop`, `on_subscribers`, `on_remove` receiving `db`, `settings`, `poller`, `bot`
  as aiogram workflow data; `class Decision(CallbackData, prefix="sub")` with `action: str`,
  `chat_id: int`; `status_keyboard()`; `approval_keyboard(chat_id: int)`.

- [ ] **Step 1: Write the failing subscription tests**

`tests/test_subscriptions.py`:

```python
from lxbot.bot import subscriptions
from lxbot.bot.subscriptions import StartOutcome
from lxbot.storage.db import SubscriberStatus

ADMIN = 1


async def start(db, chat_id):
    return await subscriptions.start(db, chat_id, "Оля", "olya", ADMIN)


async def test_new_user_requests_access(db):
    assert await start(db, 42) is StartOutcome.REQUESTED
    assert (await db.get_subscriber(42)).status is SubscriberStatus.PENDING
    assert await start(db, 42) is StartOutcome.PENDING


async def test_admin_is_subscribed_at_once(db):
    assert await start(db, ADMIN) is StartOutcome.SUBSCRIBED
    assert await subscriptions.is_approved(db, ADMIN)


async def test_approve_and_already_subscribed(db):
    await start(db, 42)
    approved = await subscriptions.decide(db, 42, approve=True)
    assert approved.status is SubscriberStatus.APPROVED
    assert await start(db, 42) is StartOutcome.ALREADY


async def test_decide_twice_is_noop(db):
    await start(db, 42)
    assert await subscriptions.decide(db, 42, approve=False) is not None
    assert await subscriptions.decide(db, 42, approve=True) is None
    assert (await db.get_subscriber(42)).status is SubscriberStatus.REJECTED


async def test_rejected_user_cannot_request_again(db):
    await start(db, 42)
    await subscriptions.decide(db, 42, approve=False)
    assert await start(db, 42) is StartOutcome.REJECTED


async def test_stop_then_start_resubscribes_without_approval(db):
    await start(db, 42)
    await subscriptions.decide(db, 42, approve=True)
    await subscriptions.stop(db, 42)
    assert not await subscriptions.is_approved(db, 42)
    assert await start(db, 42) is StartOutcome.SUBSCRIBED


async def test_removed_user_needs_nothing_and_cannot_return(db):
    await start(db, 42)
    await subscriptions.decide(db, 42, approve=True)
    removed = await subscriptions.remove(db, 42)
    assert removed.status is SubscriberStatus.REJECTED
    assert await start(db, 42) is StartOutcome.REJECTED
    assert await subscriptions.remove(db, 7) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_subscriptions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.bot.subscriptions'`

- [ ] **Step 3: Implement `lxbot/bot/subscriptions.py`**

```python
from enum import StrEnum

from lxbot.storage.db import Database, Subscriber, SubscriberStatus


class StartOutcome(StrEnum):
    SUBSCRIBED = "subscribed"
    ALREADY = "already"
    PENDING = "pending"
    REQUESTED = "requested"
    REJECTED = "rejected"


async def start(db: Database, chat_id: int, name: str, username: str | None, admin_chat_id: int) -> StartOutcome:
    existing = await db.get_subscriber(chat_id)
    status = existing.status if existing else None
    if status is SubscriberStatus.APPROVED:
        return StartOutcome.ALREADY
    if status is SubscriberStatus.PENDING:
        return StartOutcome.PENDING
    if status is SubscriberStatus.REJECTED:
        return StartOutcome.REJECTED
    # A user who left with /stop was approved before, so coming back needs no new approval.
    if chat_id == admin_chat_id or status is SubscriberStatus.INACTIVE:
        await db.save_subscriber(chat_id, name, username, SubscriberStatus.APPROVED)
        return StartOutcome.SUBSCRIBED
    await db.save_subscriber(chat_id, name, username, SubscriberStatus.PENDING)
    return StartOutcome.REQUESTED


async def decide(db: Database, chat_id: int, approve: bool) -> Subscriber | None:
    subscriber = await db.get_subscriber(chat_id)
    if subscriber is None or subscriber.status is not SubscriberStatus.PENDING:
        return None
    status = SubscriberStatus.APPROVED if approve else SubscriberStatus.REJECTED
    await db.set_subscriber_status(chat_id, status)
    return await db.get_subscriber(chat_id)


async def stop(db: Database, chat_id: int) -> None:
    if await is_approved(db, chat_id):
        await db.set_subscriber_status(chat_id, SubscriberStatus.INACTIVE)


async def remove(db: Database, chat_id: int) -> Subscriber | None:
    if await db.get_subscriber(chat_id) is None:
        return None
    await db.set_subscriber_status(chat_id, SubscriberStatus.REJECTED)
    return await db.get_subscriber(chat_id)


async def is_approved(db: Database, chat_id: int) -> bool:
    subscriber = await db.get_subscriber(chat_id)
    return subscriber is not None and subscriber.status is SubscriberStatus.APPROVED
```

- [ ] **Step 4: Run them to verify they pass**

Run: `.venv/bin/pytest tests/test_subscriptions.py -v`
Expected: 7 passed

- [ ] **Step 5: Write the failing handler tests**

`tests/test_handlers.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lxbot.bot import texts
from lxbot.bot.handlers import Decision, on_decision, on_start, on_status
from lxbot.monitor.detector import initial_state
from lxbot.storage.db import SubscriberStatus
from tests.conftest import make_settings
from tests.factories import make_snapshot

ADMIN = 1


def message(chat_id: int, name: str = "Оля <3", username: str | None = "olya"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=chat_id, full_name=name, username=username),
        answer=AsyncMock(),
    )


async def test_start_sends_an_escaped_request_to_the_admin(db):
    msg, bot = message(42), SimpleNamespace(send_message=AsyncMock())
    await on_start(msg, db=db, settings=make_settings(ADMIN), bot=bot)
    msg.answer.assert_awaited_once_with(texts.REQUEST_SENT)
    chat_id, text = bot.send_message.await_args.args
    assert chat_id == ADMIN
    assert "Оля &lt;3" in text
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None


async def test_admin_approval_notifies_the_user(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN),
        message=SimpleNamespace(html_text="🆕 Новий запит", edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    await on_decision(query, Decision(action="approve", chat_id=42), db=db, settings=make_settings(ADMIN), bot=bot)
    assert (await db.get_subscriber(42)).status is SubscriberStatus.APPROVED
    assert bot.send_message.await_args.args == (42, texts.SUBSCRIBED)
    assert texts.APPROVED_MARK in query.message.edit_text.await_args.args[0]


async def test_decision_from_a_non_admin_is_ignored(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    query = SimpleNamespace(from_user=SimpleNamespace(id=99), message=None, answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    await on_decision(query, Decision(action="approve", chat_id=42), db=db, settings=make_settings(ADMIN), bot=bot)
    assert (await db.get_subscriber(42)).status is SubscriberStatus.PENDING
    bot.send_message.assert_not_awaited()


async def test_status_requires_approval(db):
    msg = message(42)
    poller = SimpleNamespace(latest=None, state=None)
    await on_status(msg, db=db, settings=make_settings(ADMIN), poller=poller)
    msg.answer.assert_awaited_once_with(texts.NOT_SUBSCRIBED)


async def test_status_for_an_approved_user(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.APPROVED)
    snapshot = make_snapshot(0, soc=80)
    poller = SimpleNamespace(latest=snapshot, state=initial_state(snapshot))
    msg = message(42)
    await on_status(msg, db=db, settings=make_settings(ADMIN), poller=poller)
    assert "🔋 Батарея: 80%" in msg.answer.await_args.args[0]
```

- [ ] **Step 6: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_handlers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.bot.handlers'`

- [ ] **Step 7: Implement `lxbot/bot/handlers.py`**

```python
import contextlib
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from lxbot.bot import subscriptions, texts
from lxbot.bot.subscriptions import StartOutcome
from lxbot.config import Settings
from lxbot.storage.db import Database, SubscriberStatus

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)


class Decision(CallbackData, prefix="sub"):
    action: str
    chat_id: int


def status_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=texts.STATUS_BUTTON)]], resize_keyboard=True)


def approval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Схвалити", callback_data=Decision(action="approve", chat_id=chat_id).pack()),
                InlineKeyboardButton(text="❌ Відхилити", callback_data=Decision(action="reject", chat_id=chat_id).pack()),
            ]
        ]
    )


@router.message(CommandStart())
async def on_start(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    user = message.from_user
    chat_id = message.chat.id
    outcome = await subscriptions.start(db, chat_id, user.full_name, user.username, settings.admin_chat_id)
    match outcome:
        case StartOutcome.SUBSCRIBED:
            await message.answer(texts.SUBSCRIBED, reply_markup=status_keyboard())
        case StartOutcome.ALREADY:
            await message.answer(texts.ALREADY_SUBSCRIBED, reply_markup=status_keyboard())
        case StartOutcome.PENDING:
            await message.answer(texts.REQUEST_PENDING)
        case StartOutcome.REJECTED:
            await message.answer(texts.REQUEST_REJECTED)
        case StartOutcome.REQUESTED:
            await message.answer(texts.REQUEST_SENT)
            await bot.send_message(
                settings.admin_chat_id,
                texts.admin_request_text(user.full_name, user.username, chat_id),
                reply_markup=approval_keyboard(chat_id),
            )


@router.callback_query(Decision.filter(F.action.in_({"approve", "reject"})))
async def on_decision(query: CallbackQuery, callback_data: Decision, db: Database, settings: Settings, bot: Bot) -> None:
    if query.from_user.id != settings.admin_chat_id:
        await query.answer()
        return
    approve = callback_data.action == "approve"
    subscriber = await subscriptions.decide(db, callback_data.chat_id, approve)
    if subscriber is None:
        await query.answer(texts.ALREADY_DECIDED)
        return
    mark = texts.APPROVED_MARK if approve else texts.REJECTED_MARK
    await query.message.edit_text(f"{query.message.html_text}\n\n{mark}")
    with contextlib.suppress(TelegramForbiddenError):
        if approve:
            await bot.send_message(subscriber.chat_id, texts.SUBSCRIBED, reply_markup=status_keyboard())
        else:
            await bot.send_message(subscriber.chat_id, texts.REQUEST_REJECTED)
    await query.answer()


@router.message(Command("status"))
@router.message(F.text == texts.STATUS_BUTTON)
async def on_status(message: Message, db: Database, settings: Settings, poller) -> None:
    if not await subscriptions.is_approved(db, message.chat.id):
        await message.answer(texts.NOT_SUBSCRIBED)
        return
    await message.answer(texts.status_text(poller.latest, poller.state, datetime.now(UTC), settings.tz))


@router.message(Command("stop"))
async def on_stop(message: Message, db: Database) -> None:
    await subscriptions.stop(db, message.chat.id)
    await message.answer(texts.UNSUBSCRIBED, reply_markup=ReplyKeyboardRemove())


@router.message(Command("subscribers"))
async def on_subscribers(message: Message, db: Database, settings: Settings) -> None:
    if message.chat.id != settings.admin_chat_id:
        return
    subscribers = await db.subscribers(SubscriberStatus.APPROVED, SubscriberStatus.PENDING)
    if not subscribers:
        await message.answer(texts.NO_SUBSCRIBERS)
        return
    rows = [
        [InlineKeyboardButton(text=f"❌ {s.name}", callback_data=Decision(action="remove", chat_id=s.chat_id).pack())]
        for s in subscribers
        if s.chat_id != settings.admin_chat_id
    ]
    await message.answer(
        texts.subscribers_text(subscribers),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@router.callback_query(Decision.filter(F.action == "remove"))
async def on_remove(query: CallbackQuery, callback_data: Decision, db: Database, settings: Settings) -> None:
    if query.from_user.id != settings.admin_chat_id:
        await query.answer()
        return
    subscriber = await subscriptions.remove(db, callback_data.chat_id)
    await query.answer(texts.removed_text(subscriber.name) if subscriber else texts.NOT_FOUND)
```

- [ ] **Step 8: Run all tests to verify they pass**

Run: `.venv/bin/pytest tests/test_subscriptions.py tests/test_handlers.py -v`
Expected: 12 passed

- [ ] **Step 9: Commit**

```bash
git add lxbot/bot/subscriptions.py lxbot/bot/handlers.py tests/test_subscriptions.py tests/test_handlers.py
git commit -m "Add admin-approved subscriptions and bot handlers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Entry point and calibration tool

**Files:**
- Create: `lxbot/main.py`, `tools/__init__.py` (empty), `tools/dump_registers.py`
- Test: `tests/test_dump_registers.py`

**Interfaces:**
- Consumes: everything above. `settings_from_env`, `dongle_config_from_env` (Task 9);
  `DongleClient` (Task 3); `BLOCK_SIZE`, `decode_snapshot` (Task 2); `Database` (Task 5);
  `Notifier` (Task 7); `Poller` (Task 8); `router` (Task 10); `FakeDongle` (Task 3).
- Produces: `python -m lxbot.main` (service entry point); `python -m tools.dump_registers`
  (reads `DONGLE_*` / `INVERTER_SERIAL` from the environment); `tools.dump_registers.dump(env) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/test_dump_registers.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_dump_registers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools'`

- [ ] **Step 3: Implement `tools/dump_registers.py`**

```python
"""Print every input register once, raw and decoded, to check the register map on this inverter.

Stop the bot first: the dongle serves one local client at a time.
"""

import asyncio
import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime

from lxbot.config import dongle_config_from_env
from lxbot.luxpower.client import DongleClient
from lxbot.luxpower.registers import BLOCK_SIZE, InvalidSnapshot, decode_snapshot

DUMP_BLOCKS = (0, 40, 80, 120, 160)


async def dump(env: Mapping[str, str]) -> None:
    client = DongleClient(dongle_config_from_env(env))
    registers: dict[int, int] = {}
    try:
        for start in DUMP_BLOCKS:
            registers.update(enumerate(await client.read_input(start, BLOCK_SIZE), start))
    finally:
        await client.close()

    print("Raw input registers (non-zero): reg, value, hex")
    for reg, value in sorted(registers.items()):
        if value:
            print(f"  {reg:3d}  {value:6d}  {value:#06x}")

    try:
        snapshot = decode_snapshot(registers, datetime.now(UTC))
    except InvalidSnapshot as exc:
        print(f"\nCannot decode: {exc}")
        return
    print("\nDecoded:")
    for name, value in asdict(snapshot).items():
        print(f"  {name:14s} {value}")
    print(f"  {'status_name':14s} {snapshot.status_name}")
    print(f"  {'grid_present':14s} {snapshot.grid_present}")
    print(f"  {'gen_running':14s} {snapshot.gen_running}")


if __name__ == "__main__":
    asyncio.run(dump(os.environ))
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_dump_registers.py -v`
Expected: 1 passed

- [ ] **Step 5: Implement `lxbot/main.py`**

```python
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from lxbot.bot.handlers import router
from lxbot.config import Settings, settings_from_env
from lxbot.luxpower.client import DongleClient
from lxbot.monitor.poller import Poller
from lxbot.notify.broadcast import Notifier
from lxbot.storage.db import Database

COMMANDS = [
    BotCommand(command="start", description="Підписатися на сповіщення"),
    BotCommand(command="status", description="Стан мережі, генератора і батареї"),
    BotCommand(command="stop", description="Відписатися"),
]


async def run(settings: Settings) -> None:
    db = await Database.open(settings.db_path)
    client = DongleClient(settings.dongle)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db, settings.admin_chat_id, settings.tz)
    poller = Poller(client, db, notifier, settings.thresholds, settings.poll_interval)
    dispatcher = Dispatcher(db=db, settings=settings, poller=poller)
    dispatcher.include_router(router)
    background = [asyncio.create_task(notifier.run()), asyncio.create_task(poller.run())]
    try:
        await bot.set_my_commands(COMMANDS)
        await dispatcher.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await client.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(settings_from_env()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Check that the entry point imports and fails clearly without configuration**

Run: `env -i PATH="$PATH" .venv/bin/python -m lxbot.main`
Expected: exits with `lxbot.config.ConfigError: missing environment variables: BOT_TOKEN, ADMIN_CHAT_ID, DONGLE_HOST, DONGLE_SERIAL, INVERTER_SERIAL`

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: 107 passed.

- [ ] **Step 8: Commit**

```bash
git add lxbot/main.py tools/__init__.py tools/dump_registers.py tests/test_dump_registers.py
git commit -m "Add service entry point and register dump tool

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Docker packaging and README

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `.dockerignore`, `.env.example`, `README.md`

**Interfaces:**
- Consumes: `python -m lxbot.main`, `python -m tools.dump_registers` (Task 11); environment
  variables from Task 9.
- Produces: `docker compose up -d --build` runs the bot with its database in `./data`;
  `docker compose run --rm bot python -m tools.dump_registers` runs the calibration dump.

- [ ] **Step 1: Write the packaging files**

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lxbot ./lxbot
COPY tools ./tools
CMD ["python", "-m", "lxbot.main"]
```

`compose.yaml`:

```yaml
services:
  bot:
    build: .
    env_file: .env
    environment:
      DB_PATH: /app/data/bot.sqlite3
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
```

`.dockerignore`:

```
.git
.venv
.env
data
tests
docs
**/__pycache__
.pytest_cache
```

`.env.example`:

```
# Token from @BotFather
BOT_TOKEN=
# Your Telegram user id (ask @userinfobot); this user approves subscribers
ADMIN_CHAT_ID=
DONGLE_HOST=inverter.lan
DONGLE_PORT=8000
# 10-character serials of the WiFi dongle and of the inverter (LuxPower app, device list)
DONGLE_SERIAL=
INVERTER_SERIAL=
POLL_INTERVAL=15
SOC_LOW=30
SOC_CRITICAL=15
SOC_GEN_FULL=90
TZ=Europe/Kyiv
```

- [ ] **Step 2: Write `README.md`**

````markdown
# luxpower-tg-bot

Telegram bot (Ukrainian UI) for a LuxPower SNA6000 WPV inverter. It reads the inverter locally
through the WiFi dongle (TCP 8000) and tells subscribers when the grid goes or comes back, when
to start or stop the generator, and answers `/status`.

Design: `docs/superpowers/specs/2026-10-15-luxpower-tg-bot-design.md`.

## Commands

| Command | Who | What |
|---------|-----|------|
| `/start` | anyone | ask for access; the admin gets ✅ / ❌ buttons |
| `/status`, 📊 Статус | subscribers | grid, generator, battery, load, PV |
| `/stop` | subscribers | unsubscribe (`/start` brings you back without approval) |
| `/subscribers` | admin | list subscribers, remove one |

## Run

```bash
git clone https://github.com/vsguts/luxpower-tg-bot.git
cd luxpower-tg-bot
cp .env.example .env    # fill in the token, admin id and serials
docker compose up -d --build
docker compose logs -f
```

Update: `git pull && docker compose up -d --build`. The SQLite database (subscribers, event
journal, detector state) is in `./data`; back it up.

## One local client only

The dongle serves one local TCP client at a time; a second one (SolarAssistant, Home Assistant,
lxp-bridge, the dump tool) makes both fail. Stop the bot before running anything else against it.

## Calibration

The register map comes from EG4 off-grid units and is not yet confirmed on the SNA6000 WPV.
Compare the dump with the inverter display in three situations: grid on, grid off, generator running.

```bash
docker compose stop bot
docker compose run --rm bot python -m tools.dump_registers
docker compose start bot
```

Check: SOC, battery voltage, battery charge/discharge, load, PV, grid voltage, generator voltage,
`grid_present`, `gen_running`. Fix `lxbot/luxpower/registers.py` if anything disagrees.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Test fixtures use fake serials; never commit real serials, addresses or tokens (the repository is public).
````

- [ ] **Step 3: Verify the image builds (if Docker is available on the Mac)**

Run: `docker compose config --quiet && docker build -t luxpower-tg-bot:test .`
Expected: the build finishes. If Docker is not installed locally, skip; Task 13 builds on rock2.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile compose.yaml .dockerignore .env.example README.md
git commit -m "Add Docker packaging and README

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Deploy to rock2 and first calibration (operations, needs the user)

Not code: every step that changes something outside the repository needs the user's explicit OK.

- [ ] **Step 1: Push the branch to GitHub** (ask first; the repository is public). Before pushing,
  scan for leaks: `git grep -n -E 'DG6|62530|192\.168\.' -- . ':!docs'` must print nothing.
- [ ] **Step 2: Make sure `inverter.lan` resolves** from rock2 (the separate mikrotirk change
  "Static DHCP lease + inverter.lan"): `ssh rock2.lan 'getent hosts inverter.lan'`. Until it does,
  `DONGLE_HOST` can be the dongle's IP.
- [ ] **Step 3: Clone on rock2**: `ssh rock2.lan 'git clone https://github.com/vsguts/luxpower-tg-bot.git /root/luxpower-tg-bot'`.
- [ ] **Step 4: The user creates `.env`** on rock2 by hand (token and ids are secrets; they stay
  out of the transcript): create the bot in @BotFather, get the user id from @userinfobot, take the
  serials from the LuxPower app, then `ssh rock2.lan`, `cd /root/luxpower-tg-bot`,
  `cp .env.example .env`, edit, `chmod 600 .env`.
- [ ] **Step 5: Calibration dump** (grid on): `ssh rock2.lan 'cd /root/luxpower-tg-bot && docker compose run --rm --build bot python -m tools.dump_registers'`.
  Compare SOC, battery V, load and PV with the inverter display; fix the map (new task) if they disagree.
- [ ] **Step 6: Start**: `ssh rock2.lan 'cd /root/luxpower-tg-bot && docker compose up -d --build && sleep 20 && docker compose logs --tail 50'`.
  Expected: polling logs without `poll failed` warnings.
- [ ] **Step 7: Smoke test in Telegram**: the admin sends `/start` (subscribed at once) and `/status`
  (real values); a second person sends `/start`, the admin approves, the person gets the 📊 button.
- [ ] **Step 8: Record the service** in the mikrotirk repository: `docs/rock64.md`, rock2 section
  (service table: luxpower-tg-bot, `/root/luxpower-tg-bot`, compose, data in `./data`); commit there
  with explicit paths. Backing up `./data` with `rock-backup` is a separate mikrotirk change.
- [ ] **Step 9: Outage and generator calibration** at the next scheduled outage: run the dump with
  the grid off and with the generator running (stop the bot first), confirm `grid_present` /
  `gen_running` and that `GRID_LOST` / `GRID_RESTORED` arrived on time.
