# LuxPower Telegram bot — design

Date: 2026-10-15. Status: approved design, implementation not started.

## Goal

A home Telegram bot (Ukrainian UI) that reads a LuxPower SNA6000 WPV inverter locally through its
WiFi dongle and tells subscribers about grid outages, generator use and battery state, and answers
a status request on demand.

### Features (v1)

1. Grid lost notification.
2. Grid restored notification; if the generator was running, add "the generator can be turned off".
3. Battery at 30% while off-grid without the generator: hint to start the generator.
4. Battery at 15% while off-grid without the generator: critical warning.
5. Generator running and battery charged to 90%: the generator can be turned off.
6. `/status` on demand: grid, generator, SOC, battery power, load, PV.
7. Subscription by `/start`, approved by the admin; `/stop` to unsubscribe.
8. Event journal in SQLite from day one (feeds future statistics).

### Out of scope (later)

- DTEK outage schedule.
- Weekly statistics and charts.
- Remote control of the inverter (the bot never writes registers).

### Environment assumptions

- The generator is connected to the inverter's GEN port.
- During an outage the network, the host and the ISP equipment stay powered from the inverter, so the
  bot keeps running and online.
- The bot runs on a home server (rock2, Armbian, Docker Compose), deployed by `git clone`.
- The dongle is reachable from the server over TCP 8000 in clear text (verified by a passive capture).
- The repository is public: serial numbers, addresses, tokens and chat ids live only in `.env`.

## Architecture

One Python service (asyncio), one container.

```
luxpower/   client.py     dongle TCP client: frame build/parse, CRC, read input registers
            registers.py  SNA input register map -> Snapshot
monitor/    detector.py   pure logic: (State, Snapshot) -> (State, [Event])
            poller.py     polling loop: client -> detector -> storage -> notifier
bot/        handlers.py   /start, /stop, /status, admin approval, /subscribers
            texts.py      Ukrainian message templates
storage/    db.py         SQLite: subscribers, events, detector state
notify/     broadcast.py  send events to approved subscribers, admin-only alerts
config.py   settings from environment
main.py     wires the poller and the bot into one event loop
tools/dump_registers.py   calibration: print every decoded register once
```

Each unit is testable alone: the detector and the frame codec without I/O, storage against a temp
SQLite file, handlers with a fake bot.

## Dongle protocol

LuxPower "A1 1A" framing over TCP 8000, little-endian:

```
A1 1A | proto u16 | frame_len u16 (bytes after this field) | 01 | tcp_func u8 | dongle serial (10 ASCII)
data_len u16 | data_frame | CRC16
data_frame (request) = action 00 | func 04 | inverter serial (10) | start reg u16 | count u16
data_frame (response) = action 01 | func | inverter serial | start reg u16 | byte count u8 | u16 values
```

- `tcp_func` 0xC2 = translated data, 0xC1 = heartbeat.
- CRC-16/MODBUS (init 0xFFFF, poly 0xA001) over `data_frame`, stored little-endian.
- `func | 0x80` in a response is a Modbus exception.
- Reads are 40 registers per request (safe on all firmware).

References: joyfulhouse/pylxpweb (`transports/dongle.py`), celsworth/lxp-bridge (`src/lxp/packet.rs`,
wiki "TCP Packet Spec").

### Client rules

- **The dongle tolerates one local client.** The bot is the only local reader; nothing else
  (SolarAssistant, Home Assistant, lxp-bridge) may connect to port 8000.
- Cloud traffic and heartbeats share the socket. Before each request, drain pending bytes; accept
  only a reply whose `tcp_func`, function, inverter serial and start register match the request;
  skip everything else until the timeout.
- Timeout 5 s per request. On error or timeout, close the socket; the next poll reconnects, so the
  poll interval (15 s) is the reconnect backoff.
- A frame with a bad CRC or an implausible length is skipped, not fatal.
- Read-only: the client has no write function.

## Register map (input registers, func 0x04)

Blocks read every poll: 0-39, 40-79, 120-159, 160-199.

| Reg | Field | Scale |
|-----|-------|-------|
| 0 | status code | enum |
| 4 | battery voltage | /10 V |
| 5 | SOC (low byte), SOH (high byte) | % |
| 7, 8, 9 | PV1-3 power | W |
| 10 / 11 | battery charge / discharge power | W |
| 12 / 15 | grid voltage / frequency | /10 V, /100 Hz |
| 20 / 23 / 24 | EPS output voltage / frequency / power | /10 V, /100 Hz, W |
| 77 bit 0 | AC input source: 0 grid, 1 generator | bit |
| 121 / 122 | generator voltage / frequency | /10 V, /100 Hz |
| 170 | load power (signed) | W |

Status codes: 0x00 standby, 0x11 bypass, 0x19 PV charge + bypass, 0x20 AC charge, 0x28 PV & AC
charge, 0x40 battery off-grid, 0x80 PV off-grid, 0xC0 PV & battery off-grid (full list in
`registers.py`).

**Unverified on this model.** The SNA map comes from EG4 split-phase units; this inverter reports
device type `6` in holding register 19, which is not a known SNA code. Register 123 is not generator
power on SNA, and there is no local grid power register. Every field above is confirmed by
calibration before the detector relies on it.

`Snapshot` (frozen dataclass): `taken_at`, `soc`, `battery_v`, `battery_w` (+charge / -discharge),
`grid_v`, `grid_hz`, `gen_v`, `gen_hz`, `ac_source_gen`, `status`, `load_w`, `eps_w`, `pv_w`.

Derived:

- `grid_present = grid_v > 180 and grid_hz > 45`; the detector keeps a present grid down to 170 V
  (hysteresis against a sagging feeder)
- `ac_input_present = status in {0x11, 0x19, 0x20, 0x28}` (bypass or AC charge)
- `gen_running = (gen_v > 180 and gen_hz > 45) or (ac_source_gen and ac_input_present)`

Thresholds are named constants; calibration may change which signal wins.

A snapshot with battery voltage below 30 V or SOC above 100 is rejected as invalid (an empty or
garbled block) and counts as a failed poll, so a glitch never triggers a battery alert.

## Detector

Pure function `detect(state, snapshot) -> (state, events)`; `State` is persisted after every poll.

**Debounce.** A boolean (`grid_present`, `gen_running`) changes only after 3 consecutive polls agree
(about 45 s at a 15 s interval). The event time is the first of those polls.

| Event | Condition | Sent to |
|-------|-----------|---------|
| `GRID_LOST` | grid 1 -> 0 | subscribers |
| `GRID_RESTORED` | grid 0 -> 1; flag `gen_was_running` | subscribers |
| `BATTERY_LOW` | SOC <= 30, no grid, generator off; re-armed at SOC >= 35 or grid back | subscribers |
| `BATTERY_CRITICAL` | SOC <= 15, no grid, generator off; re-armed at SOC >= 20 or grid back | subscribers |
| `GEN_CHARGED` | generator running and SOC >= 90; once per generator run; not sent if grid is present | subscribers |
| `GEN_STARTED` / `GEN_STOPPED` | generator 0 <-> 1 | journal only |
| `DONGLE_DOWN` / `DONGLE_UP` | no valid snapshot for 5 min / first one after that | admin only |

- A battery alert needs 2 consecutive polls at or below its threshold (no grid, generator off), so a
  single garbled SOC reading sends nothing; the event carries the confirming poll's time and SOC.
- If the battery is already below a threshold when the grid is lost, that alert fires once, one poll
  after `GRID_LOST`; when both thresholds are crossed at once, only `BATTERY_CRITICAL` is sent.
- First start (no stored state): grid and generator state are taken from the first snapshot without
  an event; battery alerts already count that snapshot.
- After a restart: continue from the stored state; a change that happened while the bot was down is
  reported once, with the time the bot noticed it.
- While the dongle is down the detector does nothing; it never reports a grid change from missing data.

## Bot

Telegram long polling (no inbound ports).

- `/start` by an unknown user: store as `pending`, reply "Запит надіслано адміністратору", send the
  admin a card "Новий запит: <name> @<username>" with buttons **✅ Схвалити** / **❌ Відхилити**.
  Approval sends the user a confirmation and a reply keyboard with **📊 Статус**.
- Users that are not approved get no data from any command.
- `/status` (or the button):

  ```
  ⚡ Мережа: є · 229 В            (or: немає вже 2 год 15 хв)
  ⛽ Генератор: не працює
  🔋 Батарея: 76% · заряджається 1.2 кВт
  🏠 Споживання: 850 Вт
  ☀️ Сонце: 0 Вт
  🕐 Оновлено 21:14:05
  ```

  Built from the latest snapshot; if it is older than 2 minutes, say so.
- `/stop`: unsubscribe.
- Admin `/subscribers`: list with a remove button per user.
- A 403 from Telegram (user blocked the bot) marks the subscriber inactive.

### Notification texts

- `GRID_LOST`: "🔴 **10:18 Світло зникло** · Воно було 16 год 52 хв · 🔋 87%"
- `GRID_RESTORED`: "🟢 **14:02 Світло з'явилось** · Його не було 3 год 44 хв", plus
  "⛽ Генератор можна вимикати" when the generator was running.
- `BATTERY_LOW`: "🪫 **Залишилось 30%** · Час заводити генератор"
- `BATTERY_CRITICAL`: "🚨 **Залишилось 15%** · Терміново заведіть генератор"
- `GEN_CHARGED`: "🔋 **Заряджено 90%** · Генератор можна вимикати"
- `DONGLE_DOWN` / `DONGLE_UP`: "⚠️ Немає зв'язку з інвертором" / "✅ Зв'язок з інвертором відновлено"

Times are local (`Europe/Kyiv`). Sending retries on network errors; the text always carries the event
time, not the delivery time.

## Storage (SQLite)

- `subscribers(chat_id PK, name, username, status [pending|approved|rejected|inactive], created_at, updated_at)`
- `events(id PK, at, type, payload JSON)`
- `state(key PK, value JSON)` — detector state

The database lives in `./data` (a Docker volume) and is backed up by the host's backup job.

## Configuration (`.env`, git-ignored; `.env.example` committed)

`BOT_TOKEN`, `ADMIN_CHAT_ID`, `DONGLE_HOST`, `DONGLE_PORT=8000`, `DONGLE_SERIAL`, `INVERTER_SERIAL`,
`POLL_INTERVAL=15`, `SOC_LOW=30`, `SOC_CRITICAL=15`, `SOC_GEN_FULL=90`, `TZ=Europe/Kyiv`.

## Deployment

- `git clone` into `/root/luxpower-tg-bot` on rock2, create `.env`, `docker compose up -d --build`.
- Image `python:3.12-slim` (arm64); dependencies `aiogram` 3 and `aiosqlite`; `restart: unless-stopped`.
- Update: `git pull && docker compose up -d --build`.

Home network changes, done in the `mikrotirk` repository with its own change scripts:

- static DHCP lease for the dongle and a DNS name (`inverter.lan`);
- back up the bot's `data/` with rock2's `rock-backup` job.

## Testing

- Frame codec and CRC against recorded frames (`tests/fixtures/`, captured from the dongle).
- Register decoding into `Snapshot`.
- Table-driven detector scenarios: outage and return, flapping under the debounce, generator run with
  `GEN_CHARGED`, return of the grid while the generator runs, 30% / 15% thresholds and their re-arm,
  thresholds already crossed at outage start, restart with stored state, dongle down.
- Storage and subscription flow against a temp database.

## Calibration (before trusting the detector)

`python -m tools.dump_registers` prints every decoded field once. Run it with the grid on, during an
outage and with the generator running; compare with the inverter display, record input frames as
test fixtures, and fix the register map or the derived thresholds if they disagree.

## Risks

- **Register map mismatch** on this model (see above): mitigated by calibration.
- **Dongle firmware update** may switch the local link to TLS (reported for newer firmware):
  pylxpweb's `DongleTransport` shows how to handle it.
- **Second local client** (someone adds SolarAssistant or Home Assistant) breaks polling; documented
  in the README.
