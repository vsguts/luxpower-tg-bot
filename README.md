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
| `/subscribers` | admin | list subscribers; remove, approve, reject or restore one |

Any other message gets a short hint with the available commands.

## Battery forecast

With `BATTERY_CAPACITY_WH` set (e.g. `16000`), the bot estimates how long the battery lasts while the
house runs on it (no grid, no generator), down to `BATTERY_RESERVE_SOC` (default 5 %):

- **by power:** remaining energy ÷ current discharge power, like a charging station shows it;
- **by trend:** the charge lost since this battery-only stretch began ÷ the time it took (shown after 30
  minutes and a 2 % drop).

Both are rounded to 10 minutes. They appear in `/status`, in "Світло зникло" (by power only, since there
is no trend yet) and in the 30 % / 15 % alerts.

## Outage schedule

With `SCHEDULE_GROUP` set (e.g. `3.2`), the bot follows the DTEK KREM planned outage schedule for that
queue. The source is [outage-data-ua](https://github.com/Baskerville42/outage-data-ua) (MIT), which
scrapes DTEK every ~5 minutes; it is polled at the same pace. There are no fixed-time messages.

- When a day's schedule appears or changes, one message lists every known day from today on and says what
  happened ("Опубліковано графік на завтра", "🔄 Змінився графік на сьогодні"). Today's outages that are
  already over are struck through.
- A new version goes out once it has held for 20 minutes (`SCHEDULE_SETTLE_MINUTES`): DTEK sometimes posts
  an empty day first and the real one minutes later.
- An empty document (no outages planned, most of the summer) posts nothing.
- Schedule messages (and the admin's source alerts) between 23:00 and 07:00 arrive without a sound; grid
  and battery alerts always ring.
- Grid lost / restored messages say whether the change matches the schedule (±30 min) and what is
  expected next; `/status` shows the next planned change.
- The admin is told when the source stops updating for an hour.

## Tuya scenes

The bot can run a Tuya (Smart Life) tap-to-run scene when the grid goes, for example to switch the
thermostats off while the house runs on the battery, and optionally another one when it comes back.
The scene, and so what it switches, lives in the Smart Life app; the bot only starts it.

1. In Smart Life, create a tap-to-run scene with the actions you want.
2. On [platform.tuya.com](https://platform.tuya.com), open Cloud > your project (it must be linked to the
   Smart Life account, see its Devices tab): Overview has the Access ID and Secret, and the data center
   (Central Europe: `https://openapi.tuyaeu.com`). Put them into `.env` as `TUYA_ACCESS_ID`,
   `TUYA_ACCESS_SECRET` and `TUYA_API_URL`.
3. Find the scene ID (any device ID from the project's Devices tab leads to its home) and try the scene:

   ```bash
   docker compose run --rm -T bot python -m tools.tuya_scenes --device <device id>
   docker compose run --rm -T bot python -m tools.tuya_scenes --home <home id>
   docker compose run --rm -T bot python -m tools.tuya_scenes --run <scene id>
   ```

4. Set `TUYA_SCENE_GRID_LOST` (and `TUYA_SCENE_GRID_RESTORED`, if wanted) and restart the bot.

The scene starts together with the "Світло зникло" alert, about 45 s after the grid went (the debounce). A
failed run is retried for one to three minutes; then the admin gets a message. A grid change cancels the
retries left for the previous one, so a late "grid lost" scene never runs after the grid is back.

While the grid stays out, the grid-lost scene runs again every `TUYA_SCENE_REPEAT_MINUTES` (default 10;
`0` runs it once), so a device switched on during the outage goes off again within that time. A repeat
is skipped while the grid's return is being confirmed, and a bot restarted during an outage resumes the
repeats (not the first run). Repeats are not journaled; two failing in a row tell the admin, once per
outage. Each run is one API call: a few cents a year at Tuya's paid rates. Keep the project's IoT Core trial
extended (Cloud > Cloud Services), or the API stops answering.

## NAS on the inverter's UPS (NUT)

The bot can present the inverter as a network UPS (Network UPS Tools, port 3493), so a Synology NAS on a
separate battery (here an EcoFlow on the inverter's output) goes to Safe Mode once the inverter is about to
shut down, and the internet on that battery lasts longer. Any NUT client (`upsc`, Home Assistant, Linux
`upsmon`) can read it too.

- `ups.status` is `OL` (grid or generator; `OL CHRG` while charging), `OB` on the battery alone, and `OB LB`
  once the charge has been at or below `NUT_LOW_SOC` for two readings in a row (default: one above
  `BATTERY_RESERVE_SOC`, the inverter's cut-off, so 6 %). One garbled reading does not count, and once
  reported the low battery holds until the charge is 2 points higher or the grid is back.
- When the dongle goes silent for 2 minutes on battery (the inverter shuts down and takes it along, also
  when that happens while a grid or generator loss is still being confirmed): `OB LB` if the last charge
  was within 2 points of the threshold, or once the charge estimated from the last discharge power reaches it.
  A Wi-Fi gap on a healthy battery keeps the last reading.
- Also `battery.charge`, `battery.charge.low`, `battery.runtime` (the forecast by power), `input.voltage`,
  `ups.realpower`, maker and model.

Setup:

1. `.env`: `NUT_PORT=3493` turns the server on (empty: off). It only reads: anyone who can reach the port
   sees the inverter's state, nobody can change anything; who may reach it is up to the network's rules.
   `compose.yaml` publishes 3493, so a different port needs both changed. DSM logs in as `monuser` / `secret`
   to a UPS named `ups` and cannot be told otherwise; `NUT_USER` / `NUT_PASSWORD` exist for other clients.
2. DSM > Control Panel > Hardware & Power > UPS: enable UPS support, type "Synology UPS server", server IP
   = the bot's host, "Time before Synology NAS enters Standby Mode": "Same as server" (that is, on the low
   battery this server reports). Device Information should show LuxPower SNA6000 WPV with its charge.
3. Check from any machine: `upsc ups@<bot host>`.

Admin test: `/ups_test` (with a confirmation) makes the UPS report `OB LB` whatever the inverter does, so the
NAS really goes to Standby (Safe) Mode; `/ups_reset` (or a bot restart) returns the real readings. `/status` reminds the
admin while the test is on. How DSM leaves Safe Mode once the UPS is on line again is DSM's own behaviour;
check it on the first test.

During an outage, a NUT client counts a UPS it cannot reach for about 15 s as critical: restarting or
redeploying the bot while on battery sends the NAS to Safe Mode.

## Run

```bash
git clone https://github.com/vsguts/luxpower-tg-bot.git
cd luxpower-tg-bot
cp .env.example .env    # fill in the token, admin id and serials
docker compose up -d --build
docker compose logs -f
```

Then send `/start` to the bot from the admin account: Telegram lets a bot write only to users who
started it, so until then the access request cards cannot reach the admin.

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

## EcoFlow Delta Pro

With `DEVICE=ecoflow` the bot reads an EcoFlow Delta Pro through the EcoFlow cloud instead of a LuxPower
dongle. The grid is the station's AC input (above 180 V and 45 Hz); a generator on that input reads as the
grid being back, so `HAS_GENERATOR` defaults to `false` and the generator lines and hints are gone. Every poll
also checks that the station is online: the cloud answers an offline station with its last values, which
would hide a blackout, so an offline station counts as a lost connection instead. Credentials come from the
[EcoFlow developer portal](https://developer-eu.ecoflow.com): sign in with the account the station is
bound to, apply for developer access, then create an Access Key / Secret Key pair. The serial number
is in the EcoFlow app under the device's settings. Put them into `.env`:

```bash
ECOFLOW_ACCESS_KEY=
ECOFLOW_SECRET_KEY=
ECOFLOW_SERIAL=
# Optional: https://api-a.ecoflow.com for accounts registered in the Americas
ECOFLOW_API_URL=https://api-e.ecoflow.com
```

The Secret Key controls the station, not just reads it: keep it out of chats and repositories.

To check the field mapping (`lxbot/ecoflow/quota.py`), dump what the cloud reports, for example with the grid
off or while solar charges. The device state goes to the terminal; the file holds only the quota JSON:

```bash
docker compose run --rm -T bot python -m tools.dump_ecoflow > delta-pro-grid-on.json
```

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Test fixtures use fake serials; never commit real serials, addresses or tokens (the repository is public).
