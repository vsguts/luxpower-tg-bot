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
