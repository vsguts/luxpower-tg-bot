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

## EcoFlow Delta Pro (in progress)

EcoFlow support reads the station through the EcoFlow cloud. Credentials come from the
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

Capture what the cloud reports, in the same three situations: grid on, grid off, solar charging.
The device state goes to the terminal; the file holds only the quota JSON:

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
