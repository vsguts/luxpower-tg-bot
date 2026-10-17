# Planned outage schedule — design

Date: 2026-10-15. Status: approved design, implementation not started.
Builds on `2026-10-15-luxpower-tg-bot-design.md` (the running bot).

## Goal

Bring the DTEK planned outage schedule (графік стабілізаційних відключень) into the bot: daily digests,
plan-vs-actual on every grid change, and the next planned change in `/status`.

### Features

1. 09:00: today's schedule to all subscribers.
2. Tomorrow's schedule as soon as DTEK publishes it and it has settled (revised 2026-10-18, see
   "Schedule service"). Nothing by 22:00: "not published yet", and a separate message once it appears.
3. Grid lost: say whether it matches the schedule (late / early / on time / off schedule) and when the
   power is expected back.
4. Grid restored: say whether it matches the schedule and when the next planned outage is.
5. `/status`: the next planned change (inside a planned outage: until when; otherwise: the next outage).
6. A day's schedule changed after it was announced: a short notice with the new schedule.

### Out of scope

- Weekly plan-vs-actual statistics and charts (the stored schedules and the event journal make them
  possible later).
- Emergency outage notices (DTEK publishes them only on the site's address lookup).
- Choosing the queue from Telegram; it is configuration.

## Data source

SvitloBot has no public schedule API (its API only accepts device pings with a secret channel key; it
takes schedules from DTEK itself). The DTEK KREM site (dtek-krem.com.ua) is behind Imperva/Incapsula and
needs a headless browser.

Source: **Baskerville42/outage-data-ua** (MIT), a scraper that commits DTEK schedules as JSON about every
5 minutes:
`https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json`.
Verified on 2026-10-15 against the DTEK site for queue 3.2: today and tomorrow match, and the update time
"15.10.2026 19:45" matches.

Shape used:

```
lastUpdated: "2026-10-15T20:31:23.068Z"            # scraper run (UTC)
lastUpdateStatus: {ok: true, ...}                  # scraper health
fact.update: "15.10.2026 19:45"                    # DTEK's own update time (Kyiv)
fact.data: {"<local midnight epoch>": {"GPV3.2": {"1".."24": state}}}
```

- Slot `N` covers local hours N−1 to N (`"12"` = 11:00–12:00).
- States:
  - `yes`: power on;
  - `no`: off the whole hour;
  - `first` / `second`: off in the first / second half-hour;
  - `maybe`, `mfirst`, `msecond`: possibly off (the whole hour, or the first or second half-hour).
- Day keys are local midnights in Europe/Kyiv. Tomorrow is normally present (the schedule is published a
  day ahead).

The source sits behind a `ScheduleSource` interface so svitlo.live or an own DTEK scraper can replace it.

## Architecture

```
lxbot/schedule/
  model.py     Outage, DaySchedule, Schedule (frozen dataclasses)
  parse.py     JSON document -> Schedule for one group (pure)
  source.py    ScheduleSource protocol + HttpScheduleSource (aiohttp, ETag)
  compare.py   pure: match a grid event to the schedule; next planned change
  service.py   ScheduleService: poll, persist, detect changes, digests (clock-driven)
  annotate.py  ScheduleAnnotator: EventSink wrapper adding schedule matches to grid events
```

- The poller and the detector do not change. In `main.py` the poller's sink becomes
  `ScheduleAnnotator(notifier, service)`.
- `ScheduleService.run()` is a third background task, guarded like the poller and the notifier (a dead
  task stops the process; Docker restarts it).
- `aiohttp` (already installed by aiogram) becomes an explicit pinned dependency.

### Model

- `Outage(start: datetime, end: datetime, certain: bool)`, timezone-aware. `certain=False` comes from the
  `maybe*` states.
- `DaySchedule(group: str, day: date, outages: tuple[Outage, ...])`; the group gives the digest its
  "черга 3.2" label. Within a day, adjacent intervals of the same
  certainty are merged.
- `Schedule(group, days: dict[date, DaySchedule], dtek_updated: datetime | None, source_ok: bool,
  source_updated: datetime | None)`. The service keeps the fetch time next to it.
- `Schedule.timeline()` returns every known outage in order, merging same-certainty intervals across
  midnight. The comparison rules and `/status` use it, so "з 22:00 до 02:00" reads as one interval.

## Comparison rules

`W` = `SCHEDULE_MATCH_WINDOW` (30 min). An interval is a merged `Outage` from the timeline.

- **Grid lost at t** matches the first interval with `start − W ≤ t < end`.
  - Deviation `d = t − start`: `|d| ≤ 2 min` reads "вчасно", `d > 0` "на X пізніше", `d < 0` "на X раніше".
  - The expected return is that interval's `end`.
- **Grid restored at t** matches the first interval with `start < t ≤ end + W`, with deviation
  `d = t − end`.
- **No matching interval:** "⚠️ Не за графіком".
- **Possible intervals** (`certain=False`) match the same way and are labelled "за можливим відключенням".
- **Next planned outage**, for the restored message and `/status`: the first interval with `start > t`.
- **Schedule unknown or stale** (`fetched_at` older than 6 h): the schedule lines are left out of grid
  messages, never guessed.

## Messages (Ukrainian, Telegram HTML)

Grid messages gain lines from the attached match, under the existing text:

```
🔴 10:18 Світло зникло                     🟢 14:48 Світло з'явилось
🕓 Воно було 16 год 52 хв                   🕓 Його не було 4 год 30 хв
🔋 Заряд 87%                               ⏱ За графіком до 15:00 — на 12 хв раніше
⏱ За графіком з 10:00 — на 18 хв пізніше   📅 Наступне відключення: сьогодні з 20:30 до 24:00
⏳ Очікуємо світло о 14:00
```

- Off schedule: "⚠️ Не за графіком".
- No later outage known: "📅 Відключень за графіком більше не заплановано".
- Possible intervals: "⏱ За можливим відключенням з 18:00 — …".

Digest (09:00 today; tomorrow once it settles):

```
📅 Графік на сьогодні · четвер, 16 жовтня · черга 3.2
🕐 з 11:00 до 15:00 (~4 год)
🕐 з 20:30 до 24:00 (~3.5 год)
❔ можливо з 18:00 до 19:00
Без світла: 7 год 30 хв
```

- No outages: "✅ Відключень не заплановано".
- Tomorrow missing at 22:00: "⏳ Графік на завтра ще не опубліковано". When it appears later:
  "📅 Графік на завтра опубліковано" plus the digest body.
- Changed: "🔄 Графік на сьогодні змінився" (or "на завтра") plus the digest body.
- `/status` line:
  - inside a planned outage: "📅 За графіком світла немає до 15:00";
  - otherwise: "📅 Наступне відключення: сьогодні з 20:30 до 24:00" (or "завтра з …");
  - otherwise: "📅 Відключень за графіком не заплановано";
  - schedule unknown: no line.
- Admin only: "⚠️ Графік відключень не оновлюється з 21:05" and "✅ Графік відключень знову оновлюється".

## Schedule service

- **Polling:** every `SCHEDULE_POLL_INTERVAL` (15 min) with `If-None-Match`; a 304 keeps the current
  schedule. Timeout 20 s.
- **Persistence:**
  - the last good schedule (its days with normalized outages, the fetch time and the source metadata) is
    stored in the `state` table under `schedule`;
  - the digest flags are stored under `schedule-digests`: the day of the last morning digest, the pending
    "not published" day, and per announced day the fingerprint of what was announced.
  - On start the service loads them, so a restart without internet still knows the schedule and does not
    resend digests.
- **Why not a fixed evening time** (revised 2026-10-18 from the source history, 8-17 October): DTEK
  published tomorrow's schedule for queue 3.2 between 19:33 and 20:47 on most evenings, once only at
  00:38, and once as an empty day at 21:03 that was filled at 21:18.
- **Settling:** the service remembers, per day, when the current version of its outages first appeared.
  A version is announced once it has held for `SCHEDULE_SETTLE_MINUTES` (20), or 45 min after the churn
  began if DTEK keeps editing.
- **Today's digest** is due from `DIGEST_TODAY_AT` (09:00) until 18:00 if not sent yet. It is sent from
  the freshest schedule (refetch if the last fetch is older than 15 min; failure: use what is stored).
  Outages already over are struck through, and the total says what is still ahead.
- **Tomorrow:** from 18:00 the service polls every 5 min (until 01:00) and announces tomorrow once its
  version has settled. With nothing by `SCHEDULE_PENDING_AT` (22:00) and a healthy source, it sends "не
  опубліковано" once; the schedule then goes out as "опубліковано" whenever it settles. One that only
  appears after midnight goes out as today's (and replaces the 09:00 digest).
- **Changes:** what the family was told is kept per day as a fingerprint of its outages (an evening
  announcement carries over past midnight). When the schedule differs from it and the new version has
  settled, a "змінився" notice goes out.
- **Quiet hours:** schedule messages and source-health alerts between 23:00 and 07:00 are sent with
  `disable_notification`; grid and battery alerts never are.
- **Source health:** "down" when the fetch fails, `lastUpdateStatus.ok` is false, or `lastUpdated` is older
  than 1 h, continuously for 1 h.
  - The admin gets one "не оновлюється" message, and one "знову оновлюється" on recovery.
  - The last good schedule stays in use.
- **Journal:** digests, change notices and source health go through `Notifier.publish` as events
  (`schedule_today`, `schedule_tomorrow`, `schedule_tomorrow_pending`, `schedule_tomorrow_published`,
  `schedule_changed`, `schedule_source_down`, `schedule_source_up`). The day's outages go into the event payload for later
  statistics.

## Event model change

- `Event` gains two optional fields:
  - `schedule: DaySchedule | None`, for digests and change notices;
  - `match: ScheduleMatch | None`, for grid events.
- `ScheduleMatch(kind: "lost"|"restored", interval: Outage | None, deviation: timedelta | None,
  next_outage: Outage | None)`.
- `to_payload()` includes them in a JSON-safe form.
- `event_text` renders the extra lines; the existing texts are unchanged when `match` is None.

## Configuration (`.env`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SCHEDULE_GROUP` | empty | queue, e.g. `3.2` (read as `GPV3.2`); empty turns the feature off |
| `SCHEDULE_URL` | the kyiv-region raw URL above | source document |
| `SCHEDULE_POLL_INTERVAL` | `900` | seconds between fetches |
| `SCHEDULE_MATCH_WINDOW` | `30` | minutes of tolerance for "on schedule" |
| `DIGEST_TODAY_AT` | `09:00` | local time of today's digest |
| `SCHEDULE_PENDING_AT` | `22:00` | "not published yet" if tomorrow is still missing (18:00 or later) |
| `SCHEDULE_SETTLE_MINUTES` | `20` | how long a version must hold before it is announced |

Malformed values raise `ConfigError` naming the variable (as for the existing settings).

## Errors

- **Fetch or parse failure:** keep the last good schedule, log it, and count it toward source health.
- **Group missing from the document:** a parse error, which counts as a failure.
- **Unknown slot state:** treated as `maybe` and logged once per value, so a new state never hides an
  outage.
- **DST days:** slots are local hours. Datetimes are built with `zoneinfo`, and the 25/23-hour days come
  out right.

## Testing

- **Parser:** a trimmed copy of the real `kyiv-region.json` as a fixture (public data, no secrets), plus
  synthetic slot maps: half hours, `maybe`, merging, midnight, an unknown state.
- **Comparison:** table-driven: late, early, on time, off schedule, at the edges of the window, a possible
  interval, an interval crossing midnight, the next outage today, tomorrow and none.
- **Service with a fake clock and a fake source:**
  - the 09:00 digest and the settled announcement of tomorrow;
  - a late start;
  - no resend after a restart;
  - tomorrow pending and then published;
  - change notices only after the digest;
  - the ETag 304 path;
  - source down and up.
- **Annotator:** grid events get matches; other events pass through untouched.
- **Texts:** every message above, including `/status` lines.
- **Configuration:** defaults, the feature off, malformed values.

## Risks

- **outage-data-ua is one maintainer's scraper.** If it breaks, the source-health alert says so. The
  `ScheduleSource` interface allows a replacement.
- **Raw GitHub caching** may delay updates by a few minutes. That is acceptable: DTEK's own page says
  updates take up to 15 min.
- **Grid-change detection lags the real change by about 45 s** (the debounce). That is well inside the
  30 min window.
