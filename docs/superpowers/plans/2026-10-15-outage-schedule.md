# Planned Outage Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the DTEK planned outage schedule (queue 3.2) to the running bot: 09:00 and 21:00 digests,
plan-vs-actual lines on grid lost/restored messages, change notices and a `/status` line.

**Architecture:** A new `lxbot/schedule/` package.
- Pure parts: `model.py` (`Outage` / `DaySchedule` / `Schedule`), `parse.py` (outage-data-ua JSON →
  `Schedule`) and `compare.py` (matching events to the schedule).
- `source.py` fetches the document with aiohttp and ETag.
- `service.py` (`ScheduleService`) polls, persists to SQLite, detects changes and sends clock-driven
  digests as `Event`s through the existing `Notifier`.
- `annotate.py` (`ScheduleAnnotator`) wraps the notifier as the poller's sink and attaches a
  `ScheduleMatch` to grid events. The poller and the detector logic are unchanged.

**Tech Stack:** Python 3.12, aiogram 3.31, aiohttp 3.14 (already installed via aiogram, now pinned),
aiosqlite, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-15-outage-schedule-design.md`

## Global Constraints

- Python 3.12. The only new runtime dependency is `aiohttp==3.14.5` (already in `.venv` as an aiogram
  dependency).
- All code, comments and docs are in English; every user-facing bot text is in Ukrainian (Telegram HTML).
- The repository is public: no real serials, IPs, tokens or chat ids in git. The schedule fixture is
  public DTEK data and is fine.
- Timestamps are timezone-aware. Schedule days and slots are local `Europe/Kyiv`.
- Source: `https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json`. The
  group key is `GPV` + queue (`3.2` → `GPV3.2`). Slot `N` covers local hour N−1…N.
- Defaults: `SCHEDULE_POLL_INTERVAL=900` s, `SCHEDULE_MATCH_WINDOW=30` min, `DIGEST_TODAY_AT=09:00`,
  `DIGEST_TOMORROW_AT=21:00`. An empty `SCHEDULE_GROUP` turns the feature off.
- Stale limits:
  - the schedule is not used for grid lines and `/status` once the last fetch is older than 6 h;
  - the source counts as down when it is unhealthy for 1 h;
  - its own `lastUpdated` may be at most 1 h old.
- Deviation of up to 2 min reads "вчасно".
- Comments explain only *why*; no doc blocks on obvious functions.
- Commit with explicit paths (`git add <files>`), never `git add -A`. Every commit ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Work on branch `feat/outage-schedule` in `~/Projects/vsguts/luxpower-tg-bot`. Run tests with
  `.venv/bin/pytest -q -W error`; output must be pristine.

## Review Focus

1. **DTEK publishes tomorrow as an empty day first and fills it later.** Expect the 21:00 digest to say
   "no outages" and a later "🔄 changed" notice, not silence. Pinned by Task 5
   `test_tomorrow_filled_after_the_evening_digest_is_announced`.
2. **Bot restarted around 09:00.** Expect exactly one morning digest. Pinned by Task 5
   `test_no_resend_after_restart`.
3. **Source dead for a day.** Expect grid messages without schedule lines rather than stale claims.
   Pinned by Task 5 `test_refresh_failure_keeps_the_last_schedule_until_it_is_stale`.
4. **An outage crossing midnight.** Expect the digest to show "до 24:00" and the next day "з 00:00", while
   the match treats them as one interval. Pinned by Task 2 `test_interval_across_midnight` and Task 3
   `test_range_ending_at_midnight_reads_24_00`.
5. **A new slot state value appears.** Expect it to count as a possible outage, never dropped. Pinned by
   Task 1 `test_unknown_state_is_a_possible_outage`.

## File Structure

```
lxbot/schedule/__init__.py      empty
lxbot/schedule/model.py         Outage, DaySchedule, Schedule, merge_outages
lxbot/schedule/parse.py         parse_schedule, group_key, ScheduleParseError
lxbot/schedule/compare.py       MatchKind, ScheduleMatch, match_lost, match_restored, current_outage, next_outage
lxbot/schedule/source.py        ScheduleSource, HttpScheduleSource, ScheduleSourceError
lxbot/schedule/service.py       ScheduleConfig, ScheduleService, DEFAULT_URL
lxbot/schedule/annotate.py      ScheduleAnnotator
lxbot/monitor/detector.py       (modify) schedule EventTypes, Event.schedule / Event.match
lxbot/bot/texts.py              (modify) schedule texts, grid lines, /status line
lxbot/bot/handlers.py           (modify) on_status passes the schedule
lxbot/config.py                 (modify) Settings.schedule, schedule_config_from_env
lxbot/main.py                   (modify) wiring
tests/fixtures/kyiv-region-sample.json
tests/schedule_factories.py     KYIV, local(), make_document()
tests/test_schedule_parse.py, test_schedule_compare.py, test_schedule_texts.py,
tests/test_schedule_source.py, test_schedule_service.py, test_schedule_annotate.py
README.md, .env.example, requirements.txt (modify)
```

---

### Task 1: Schedule model and parser

**Files:**
- Create: `lxbot/schedule/__init__.py` (empty), `lxbot/schedule/model.py`, `lxbot/schedule/parse.py`
- Create: `tests/fixtures/kyiv-region-sample.json`, `tests/schedule_factories.py`
- Test: `tests/test_schedule_parse.py`

**Interfaces:**
- Produces (`lxbot.schedule.model`):
  - `Outage(start: datetime, end: datetime, certain: bool = True)` with `duration`, `to_list()` and
    `Outage.from_list(data)`;
  - `merge_outages(outages: Iterable[Outage]) -> tuple[Outage, ...]`;
  - `DaySchedule(group: str, day: date, outages: tuple[Outage, ...] = ())` with `label` (`"3.2"`),
    `certain_total: timedelta`, `to_dict()` and `DaySchedule.from_dict(data)`;
  - `Schedule(group: str, days: dict[date, DaySchedule], dtek_updated: datetime | None = None,
    source_ok: bool = True, source_updated: datetime | None = None)` with
    `day(d) -> DaySchedule | None` and `timeline() -> tuple[Outage, ...]`.
- Produces (`lxbot.schedule.parse`): `class ScheduleParseError(ValueError)`, `group_key(queue: str) -> str`,
  `parse_schedule(document: Mapping, group: str, tz: ZoneInfo) -> Schedule`.
- Produces (`tests.schedule_factories`): `KYIV`, `GROUP = "GPV3.2"`, `TODAY = date(2026, 10, 15)`,
  `TOMORROW = date(2026, 10, 16)`, `local(y, m, d, h=0, minute=0) -> datetime` (Kyiv),
  `make_slots(states: dict[int, str]) -> dict[str, str]`,
  `make_document(days: dict[date, dict[int, str]], *, group=GROUP, ok=True, last_updated: datetime | None = None, dtek_update="15.10.2026 19:45") -> dict`.

- [ ] **Step 1: Add the fixture and test factories**

`tests/fixtures/kyiv-region-sample.json` (trimmed from the real document fetched 2026-10-15; groups 3.2
and 6.2 only):

```json
{
  "regionId": "kyiv-region",
  "lastUpdated": "2026-10-15T20:31:23.068Z",
  "lastUpdateStatus": {"status": "parsed", "ok": true, "code": 200, "message": null, "at": "2026-10-15T20:31:23.068Z", "attempt": 45667},
  "fact": {
    "update": "15.10.2026 19:45",
    "today": 1792011600,
    "data": {
      "1792011600": {
        "GPV3.2": {"1":"yes","2":"yes","3":"yes","4":"yes","5":"yes","6":"yes","7":"yes","8":"yes","9":"yes","10":"yes","11":"yes","12":"yes","13":"yes","14":"yes","15":"no","16":"no","17":"no","18":"no","19":"yes","20":"yes","21":"yes","22":"yes","23":"yes","24":"yes"},
        "GPV6.2": {"1":"yes","2":"yes","3":"yes","4":"yes","5":"yes","6":"yes","7":"yes","8":"yes","9":"yes","10":"yes","11":"second","12":"no","13":"no","14":"no","15":"first","16":"yes","17":"yes","18":"second","19":"no","20":"no","21":"no","22":"first","23":"yes","24":"yes"}
      },
      "1792098000": {
        "GPV3.2": {"1":"yes","2":"yes","3":"yes","4":"yes","5":"yes","6":"yes","7":"yes","8":"yes","9":"yes","10":"yes","11":"yes","12":"no","13":"no","14":"no","15":"no","16":"yes","17":"yes","18":"yes","19":"yes","20":"yes","21":"yes","22":"yes","23":"yes","24":"yes"},
        "GPV6.2": {"1":"yes","2":"yes","3":"yes","4":"yes","5":"yes","6":"yes","7":"yes","8":"yes","9":"yes","10":"yes","11":"yes","12":"yes","13":"yes","14":"yes","15":"second","16":"no","17":"no","18":"no","19":"first","20":"yes","21":"yes","22":"yes","23":"yes","24":"yes"}
      }
    }
  }
}
```

`tests/schedule_factories.py`:

```python
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
GROUP = "GPV3.2"
TODAY = date(2026, 10, 15)
TOMORROW = date(2026, 10, 16)


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KYIV)


def day_key(day: date) -> str:
    return str(int(datetime.combine(day, time(), KYIV).timestamp()))


def make_slots(states: dict[int, str]) -> dict[str, str]:
    return {str(slot): states.get(slot, "yes") for slot in range(1, 25)}


def make_document(
    days: dict[date, dict[int, str]],
    *,
    group: str = GROUP,
    ok: bool = True,
    last_updated: datetime | None = None,
    dtek_update: str = "15.10.2026 19:45",
) -> dict:
    return {
        "lastUpdated": (last_updated or datetime(2026, 10, 16, 23, 0, tzinfo=UTC)).isoformat(),
        "lastUpdateStatus": {"ok": ok},
        "fact": {
            "update": dtek_update,
            "data": {day_key(day): {group: make_slots(states)} for day, states in days.items()},
        },
    }
```

(The default `last_updated` lies after every test clock, so the source counts as fresh unless a test says
otherwise.)

- [ ] **Step 2: Write the failing tests**

`tests/test_schedule_parse.py`:

```python
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from lxbot.schedule.model import DaySchedule, Outage
from lxbot.schedule.parse import ScheduleParseError, group_key, parse_schedule
from tests.schedule_factories import GROUP, KYIV, TODAY, TOMORROW, local, make_document

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "kyiv-region-sample.json").read_text())


def test_group_key():
    assert group_key("3.2") == "GPV3.2"


def test_real_document_for_queue_3_2():
    schedule = parse_schedule(SAMPLE, "GPV3.2", KYIV)
    assert schedule.group == "GPV3.2"
    assert schedule.days[TODAY].outages == (Outage(local(2026, 10, 15, 14), local(2026, 10, 15, 18)),)
    assert schedule.days[TOMORROW].outages == (Outage(local(2026, 10, 16, 11), local(2026, 10, 16, 15)),)
    assert schedule.dtek_updated == local(2026, 10, 15, 19, 45)
    assert schedule.source_ok is True
    assert schedule.source_updated == datetime(2026, 10, 15, 20, 31, 23, 68000, tzinfo=UTC)


def test_half_hours_merge_into_intervals():
    schedule = parse_schedule(SAMPLE, "GPV6.2", KYIV)
    assert schedule.days[TODAY].outages == (
        Outage(local(2026, 10, 15, 10, 30), local(2026, 10, 15, 14, 30)),
        Outage(local(2026, 10, 15, 17, 30), local(2026, 10, 15, 21, 30)),
    )
    assert schedule.days[TODAY].certain_total == timedelta(hours=8)


def test_possible_outages_stay_separate_from_certain_ones():
    schedule = parse_schedule(make_document({TODAY: {11: "maybe", 12: "no"}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == (
        Outage(local(2026, 10, 15, 10), local(2026, 10, 15, 11), certain=False),
        Outage(local(2026, 10, 15, 11), local(2026, 10, 15, 12)),
    )


def test_possible_half_hours():
    schedule = parse_schedule(make_document({TODAY: {11: "mfirst", 12: "msecond"}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == (
        Outage(local(2026, 10, 15, 10), local(2026, 10, 15, 10, 30), certain=False),
        Outage(local(2026, 10, 15, 11, 30), local(2026, 10, 15, 12), certain=False),
    )


def test_unknown_state_is_a_possible_outage():
    schedule = parse_schedule(make_document({TODAY: {11: "weird"}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == (Outage(local(2026, 10, 15, 10), local(2026, 10, 15, 11), certain=False),)


def test_a_day_without_outages():
    schedule = parse_schedule(make_document({TODAY: {}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == ()
    assert schedule.days[TODAY].certain_total == timedelta()


def test_timeline_merges_across_midnight():
    schedule = parse_schedule(make_document({TODAY: {24: "no"}, TOMORROW: {1: "no"}}), GROUP, KYIV)
    assert schedule.days[TODAY].outages == (Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 0)),)
    assert schedule.timeline() == (Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 1)),)


def test_missing_group_is_an_error():
    with pytest.raises(ScheduleParseError, match="GPV9.9"):
        parse_schedule(SAMPLE, "GPV9.9", KYIV)


def test_document_without_fact_is_an_error():
    with pytest.raises(ScheduleParseError):
        parse_schedule({"regionId": "kyiv-region"}, GROUP, KYIV)


def test_bad_slot_key_is_an_error():
    document = make_document({TODAY: {}})
    next(iter(document["fact"]["data"].values()))[GROUP]["x"] = "no"
    with pytest.raises(ScheduleParseError):
        parse_schedule(document, GROUP, KYIV)


def test_missing_metadata_is_tolerated():
    document = make_document({TODAY: {}})
    del document["lastUpdated"]
    document["fact"]["update"] = "soon"
    schedule = parse_schedule(document, GROUP, KYIV)
    assert schedule.source_updated is None
    assert schedule.dtek_updated is None


def test_day_schedule_round_trip_and_label():
    day = parse_schedule(SAMPLE, "GPV6.2", KYIV).days[TODAY]
    assert DaySchedule.from_dict(day.to_dict()) == day
    assert day.label == "6.2"
    assert DaySchedule("GPV3.2", date(2026, 10, 15)).outages == ()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule_parse.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.schedule'`

- [ ] **Step 4: Implement `lxbot/schedule/model.py`**

```python
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class Outage:
    start: datetime
    end: datetime
    certain: bool = True

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def to_list(self) -> list:
        return [self.start.isoformat(), self.end.isoformat(), self.certain]

    @classmethod
    def from_list(cls, data: list) -> "Outage":
        start, end, certain = data
        return cls(datetime.fromisoformat(start), datetime.fromisoformat(end), certain)


def merge_outages(outages: Iterable[Outage]) -> tuple[Outage, ...]:
    merged: list[Outage] = []
    for outage in sorted(outages, key=lambda o: (o.start, not o.certain)):
        last = merged[-1] if merged else None
        if last is not None and last.certain == outage.certain and outage.start <= last.end:
            merged[-1] = Outage(last.start, max(last.end, outage.end), last.certain)
        else:
            merged.append(outage)
    return tuple(merged)


@dataclass(frozen=True)
class DaySchedule:
    group: str
    day: date
    outages: tuple[Outage, ...] = ()

    @property
    def label(self) -> str:
        return self.group.removeprefix("GPV")

    @property
    def certain_total(self) -> timedelta:
        return sum((o.duration for o in self.outages if o.certain), timedelta())

    def to_dict(self) -> dict:
        return {"group": self.group, "day": self.day.isoformat(), "outages": [o.to_list() for o in self.outages]}

    @classmethod
    def from_dict(cls, data: dict) -> "DaySchedule":
        return cls(
            group=data["group"],
            day=date.fromisoformat(data["day"]),
            outages=tuple(Outage.from_list(o) for o in data["outages"]),
        )


@dataclass(frozen=True)
class Schedule:
    group: str
    days: dict[date, DaySchedule]
    dtek_updated: datetime | None = None
    source_ok: bool = True
    source_updated: datetime | None = None

    def day(self, day: date) -> DaySchedule | None:
        return self.days.get(day)

    # Merged across midnight, so "23:00-01:00" is one interval for matching.
    def timeline(self) -> tuple[Outage, ...]:
        return merge_outages(o for d in self.days.values() for o in d.outages)
```

- [ ] **Step 5: Implement `lxbot/schedule/parse.py`**

```python
"""outage-data-ua document (github.com/Baskerville42/outage-data-ua) -> Schedule for one group."""

import logging
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from lxbot.schedule.model import DaySchedule, Outage, Schedule, merge_outages

log = logging.getLogger(__name__)

# State -> (first minute, last minute, certain) within the slot's hour.
_SPANS = {
    "no": (0, 60, True),
    "first": (0, 30, True),
    "second": (30, 60, True),
    "maybe": (0, 60, False),
    "mfirst": (0, 30, False),
    "msecond": (30, 60, False),
}
_POWER_ON = "yes"
# A state the source adds later must not hide an outage: count it as a possible one for the hour.
_UNKNOWN_SPAN = (0, 60, False)
_DTEK_TIME_FORMAT = "%d.%m.%Y %H:%M"
_warned_states: set[str] = set()


class ScheduleParseError(ValueError):
    pass


def group_key(queue: str) -> str:
    return f"GPV{queue}"


def parse_schedule(document: Mapping, group: str, tz: ZoneInfo) -> Schedule:
    try:
        fact = document["fact"]
        data = fact["data"]
        days = {}
        for day_key, groups in data.items():
            slots = groups.get(group) if isinstance(groups, Mapping) else None
            if slots is None:
                continue
            day = datetime.fromtimestamp(int(day_key), tz).date()
            days[day] = DaySchedule(group, day, merge_outages(_slot_outages(day, slots, tz)))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ScheduleParseError(f"malformed schedule document: {exc!r}") from exc
    if not days:
        raise ScheduleParseError(f"group {group} is not in the schedule document")
    status = document.get("lastUpdateStatus") or {}
    return Schedule(
        group=group,
        days=days,
        dtek_updated=_dtek_time(fact.get("update"), tz),
        source_ok=bool(status.get("ok")),
        source_updated=_utc_time(document.get("lastUpdated")),
    )


def _slot_outages(day: date, slots: Mapping[str, str], tz: ZoneInfo) -> list[Outage]:
    outages = []
    for slot, state in slots.items():
        if state == _POWER_ON:
            continue
        span = _SPANS.get(state)
        if span is None:
            if state not in _warned_states:
                _warned_states.add(state)
                log.warning("unknown schedule state %r, counted as a possible outage", state)
            span = _UNKNOWN_SPAN
        first, last, certain = span
        hour_start = datetime.combine(day, time(int(slot) - 1), tz)
        outages.append(
            Outage(hour_start + timedelta(minutes=first), hour_start + timedelta(minutes=last), certain)
        )
    return outages


def _dtek_time(value: object, tz: ZoneInfo) -> datetime | None:
    try:
        return datetime.strptime(value, _DTEK_TIME_FORMAT).replace(tzinfo=tz)
    except (TypeError, ValueError):
        return None


def _utc_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule_parse.py -q -W error`
Expected: all pass (13). Then the full suite: `.venv/bin/pytest -q -W error`, all pass.

- [ ] **Step 7: Commit**

```bash
git add lxbot/schedule/__init__.py lxbot/schedule/model.py lxbot/schedule/parse.py \
  tests/fixtures/kyiv-region-sample.json tests/schedule_factories.py tests/test_schedule_parse.py
git commit -m "Parse the DTEK outage schedule from outage-data-ua

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Matching grid events to the schedule

**Files:**
- Create: `lxbot/schedule/compare.py`
- Test: `tests/test_schedule_compare.py`

**Interfaces:**
- Consumes: `Outage`, `merge_outages` (Task 1); `local`, `TODAY`, `TOMORROW` (Task 1 factories).
- Produces (`lxbot.schedule.compare`):
  - `class MatchKind(StrEnum)`: `LOST = "lost"`, `RESTORED = "restored"`;
  - frozen dataclass `ScheduleMatch(kind: MatchKind, interval: Outage | None = None,
    deviation: timedelta | None = None, next_outage: Outage | None = None)` with `to_payload() -> dict`;
  - `match_lost(timeline: Sequence[Outage], at: datetime, window: timedelta) -> ScheduleMatch`;
  - `match_restored(timeline, at, window) -> ScheduleMatch`;
  - `current_outage(timeline, at) -> Outage | None`;
  - `next_outage(timeline, at) -> Outage | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_schedule_compare.py`:

```python
from datetime import timedelta

import pytest

from lxbot.schedule.compare import (
    MatchKind,
    ScheduleMatch,
    current_outage,
    match_lost,
    match_restored,
    next_outage,
)
from lxbot.schedule.model import Outage, merge_outages
from tests.schedule_factories import local

WINDOW = timedelta(minutes=30)
MORNING = Outage(local(2026, 10, 15, 11), local(2026, 10, 15, 15))
POSSIBLE = Outage(local(2026, 10, 15, 18), local(2026, 10, 15, 19), certain=False)
EVENING = Outage(local(2026, 10, 15, 20, 30), local(2026, 10, 16, 0))
TIMELINE = merge_outages([EVENING, MORNING, POSSIBLE])


def at(hour, minute=0):
    return local(2026, 10, 15, hour, minute)


@pytest.mark.parametrize(
    ("hour", "minute", "interval", "deviation"),
    [
        (11, 18, MORNING, timedelta(minutes=18)),
        (10, 40, MORNING, timedelta(minutes=-20)),
        (10, 30, MORNING, timedelta(minutes=-30)),
        (14, 59, MORNING, timedelta(hours=3, minutes=59)),
        (18, 5, POSSIBLE, timedelta(minutes=5)),
        (20, 30, EVENING, timedelta()),
    ],
)
def test_lost_on_schedule(hour, minute, interval, deviation):
    assert match_lost(TIMELINE, at(hour, minute), WINDOW) == ScheduleMatch(MatchKind.LOST, interval, deviation)


@pytest.mark.parametrize(("hour", "minute"), [(10, 29), (15, 0), (16, 0), (8, 0)])
def test_lost_off_schedule(hour, minute):
    assert match_lost(TIMELINE, at(hour, minute), WINDOW) == ScheduleMatch(MatchKind.LOST)


@pytest.mark.parametrize(
    ("hour", "minute", "interval", "deviation", "following"),
    [
        (14, 48, MORNING, timedelta(minutes=-12), POSSIBLE),
        (15, 30, MORNING, timedelta(minutes=30), POSSIBLE),
        (19, 0, POSSIBLE, timedelta(), EVENING),
        (23, 50, EVENING, timedelta(minutes=-10), None),
    ],
)
def test_restored_on_schedule(hour, minute, interval, deviation, following):
    assert match_restored(TIMELINE, at(hour, minute), WINDOW) == ScheduleMatch(
        MatchKind.RESTORED, interval, deviation, following
    )


@pytest.mark.parametrize(("hour", "minute"), [(15, 31), (11, 0), (9, 0)])
def test_restored_off_schedule_still_names_the_next_outage(hour, minute):
    match = match_restored(TIMELINE, at(hour, minute), WINDOW)
    assert match.interval is None
    assert match.deviation is None
    assert match.next_outage == next_outage(TIMELINE, at(hour, minute))


def test_interval_across_midnight():
    timeline = merge_outages(
        [Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 0)), Outage(local(2026, 10, 16, 0), local(2026, 10, 16, 1))]
    )
    night = Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 1))
    assert match_lost(timeline, local(2026, 10, 15, 23, 10), WINDOW).interval == night
    restored = match_restored(timeline, local(2026, 10, 16, 0, 50), WINDOW)
    assert restored.interval == night
    assert restored.deviation == timedelta(minutes=-10)


def test_current_and_next_outage():
    assert current_outage(TIMELINE, at(12)) == MORNING
    assert current_outage(TIMELINE, at(15)) is None
    assert next_outage(TIMELINE, at(12)) == POSSIBLE
    assert next_outage(TIMELINE, at(21)) is None
    assert next_outage((), at(12)) is None


def test_payload():
    match = ScheduleMatch(MatchKind.RESTORED, MORNING, timedelta(minutes=-12), POSSIBLE)
    assert match.to_payload() == {
        "kind": "restored",
        "interval": MORNING.to_list(),
        "deviation_s": -720,
        "next_outage": POSSIBLE.to_list(),
    }
    assert ScheduleMatch(MatchKind.LOST).to_payload() == {
        "kind": "lost",
        "interval": None,
        "deviation_s": None,
        "next_outage": None,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule_compare.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.schedule.compare'`

- [ ] **Step 3: Implement `lxbot/schedule/compare.py`**

```python
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from lxbot.schedule.model import Outage


class MatchKind(StrEnum):
    LOST = "lost"
    RESTORED = "restored"


@dataclass(frozen=True)
class ScheduleMatch:
    kind: MatchKind
    interval: Outage | None = None
    deviation: timedelta | None = None
    next_outage: Outage | None = None

    def to_payload(self) -> dict:
        return {
            "kind": str(self.kind),
            "interval": self.interval.to_list() if self.interval else None,
            "deviation_s": int(self.deviation.total_seconds()) if self.deviation is not None else None,
            "next_outage": self.next_outage.to_list() if self.next_outage else None,
        }


def match_lost(timeline: Sequence[Outage], at: datetime, window: timedelta) -> ScheduleMatch:
    for outage in timeline:
        if outage.start - window <= at < outage.end:
            return ScheduleMatch(MatchKind.LOST, outage, at - outage.start)
    return ScheduleMatch(MatchKind.LOST)


def match_restored(timeline: Sequence[Outage], at: datetime, window: timedelta) -> ScheduleMatch:
    following = next_outage(timeline, at)
    for outage in timeline:
        if outage.start < at <= outage.end + window:
            return ScheduleMatch(MatchKind.RESTORED, outage, at - outage.end, following)
    return ScheduleMatch(MatchKind.RESTORED, next_outage=following)


def current_outage(timeline: Sequence[Outage], at: datetime) -> Outage | None:
    return next((o for o in timeline if o.start <= at < o.end), None)


def next_outage(timeline: Sequence[Outage], at: datetime) -> Outage | None:
    return next((o for o in timeline if o.start > at), None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule_compare.py -q -W error`
Expected: all pass. Then the full suite, all pass.

- [ ] **Step 5: Commit**

```bash
git add lxbot/schedule/compare.py tests/test_schedule_compare.py
git commit -m "Match grid changes against the outage schedule

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Schedule events and Ukrainian texts

**Files:**
- Modify: `lxbot/monitor/detector.py` (EventType, `_AUDIENCES`, `Event`)
- Modify: `lxbot/bot/texts.py`
- Test: `tests/test_schedule_texts.py`; the existing `tests/test_detector.py` and `tests/test_texts.py`
  must keep passing unchanged

**Interfaces:**
- Consumes: `DaySchedule`, `Outage`, `Schedule` (Task 1); `MatchKind`, `ScheduleMatch`, `current_outage`,
  `next_outage` (Task 2).
- Produces (`lxbot.monitor.detector`):
  - new `EventType` members:
    - `SCHEDULE_TODAY = "schedule_today"`, `SCHEDULE_TOMORROW = "schedule_tomorrow"`;
    - `SCHEDULE_TOMORROW_PENDING = "schedule_tomorrow_pending"`,
      `SCHEDULE_TOMORROW_PUBLISHED = "schedule_tomorrow_published"`;
    - `SCHEDULE_CHANGED = "schedule_changed"`;
    - `SCHEDULE_SOURCE_DOWN = "schedule_source_down"`, `SCHEDULE_SOURCE_UP = "schedule_source_up"`.
    The last two have audience ADMIN; the others SUBSCRIBERS.
  - `Event` gains `schedule: DaySchedule | None = None` and `match: ScheduleMatch | None = None`.
    `to_payload()` adds `"schedule"` / `"match"` keys only when they are set.
- Produces (`lxbot.bot.texts`):
  - `ON_TIME = timedelta(minutes=2)`;
  - `format_range(outage: Outage, tz) -> str` ("з 11:00 до 15:00", a local-midnight end reads "24:00");
  - `format_deviation(deviation: timedelta) -> str`;
  - `schedule_lines(match: ScheduleMatch, at: datetime, tz) -> list[str]`;
  - `day_schedule_text(title: str, schedule: DaySchedule, tz, icon: str = "📅") -> str`;
  - `schedule_status_line(schedule: Schedule, now: datetime, tz) -> str`;
  - `status_text(snapshot, state, now, tz, schedule: Schedule | None = None) -> str` (new optional
    parameter);
  - `event_text` renders the schedule events, and appends `schedule_lines` to GRID_LOST / GRID_RESTORED
    when `event.match` is set.

- [ ] **Step 1: Write the failing tests**

`tests/test_schedule_texts.py`:

```python
from datetime import timedelta

import pytest

from lxbot.bot import texts
from lxbot.monitor.detector import Audience, Debounced, Event, EventType, State
from lxbot.schedule.compare import MatchKind, ScheduleMatch
from lxbot.schedule.model import DaySchedule, Outage, Schedule
from tests.factories import make_snapshot
from tests.schedule_factories import GROUP, KYIV, TODAY, TOMORROW, local

MORNING = Outage(local(2026, 10, 15, 11), local(2026, 10, 15, 15))
EVENING = Outage(local(2026, 10, 15, 20, 30), local(2026, 10, 16, 0))
POSSIBLE = Outage(local(2026, 10, 15, 18), local(2026, 10, 15, 19), certain=False)
TODAY_SCHEDULE = DaySchedule(GROUP, TODAY, (MORNING, POSSIBLE, EVENING))
SCHEDULE = Schedule(GROUP, {TODAY: TODAY_SCHEDULE})


def test_range_ending_at_midnight_reads_24_00():
    assert texts.format_range(MORNING, KYIV) == "з 11:00 до 15:00"
    assert texts.format_range(EVENING, KYIV) == "з 20:30 до 24:00"
    night = Outage(local(2026, 10, 15, 23), local(2026, 10, 16, 1))
    assert texts.format_range(night, KYIV) == "з 23:00 до 01:00"


@pytest.mark.parametrize(
    ("deviation", "expected"),
    [
        (timedelta(minutes=18), "на 18 хв пізніше"),
        (timedelta(minutes=-12), "на 12 хв раніше"),
        (timedelta(minutes=2), "вчасно"),
        (timedelta(minutes=-1), "вчасно"),
        (timedelta(hours=1, minutes=5), "на 1 год 5 хв пізніше"),
    ],
)
def test_format_deviation(deviation, expected):
    assert texts.format_deviation(deviation) == expected


def test_lost_on_schedule_lines():
    match = ScheduleMatch(MatchKind.LOST, MORNING, timedelta(minutes=18))
    assert texts.schedule_lines(match, local(2026, 10, 15, 11, 18), KYIV) == [
        "⏱ За графіком з 11:00 — на 18 хв пізніше",
        "⏳ Очікуємо світло о 15:00",
    ]


def test_lost_on_a_possible_outage():
    match = ScheduleMatch(MatchKind.LOST, POSSIBLE, timedelta())
    assert texts.schedule_lines(match, local(2026, 10, 15, 18), KYIV)[0] == "⏱ За можливим відключенням з 18:00 — вчасно"


def test_lost_off_schedule():
    assert texts.schedule_lines(ScheduleMatch(MatchKind.LOST), local(2026, 10, 15, 9), KYIV) == ["⚠️ Не за графіком"]


def test_restored_lines_name_the_next_outage():
    match = ScheduleMatch(MatchKind.RESTORED, MORNING, timedelta(minutes=-12), EVENING)
    assert texts.schedule_lines(match, local(2026, 10, 15, 14, 48), KYIV) == [
        "⏱ За графіком до 15:00 — на 12 хв раніше",
        "📅 Наступне відключення: сьогодні з 20:30 до 24:00",
    ]


def test_restored_off_schedule_with_nothing_planned():
    match = ScheduleMatch(MatchKind.RESTORED)
    assert texts.schedule_lines(match, local(2026, 10, 15, 9), KYIV) == [
        "⚠️ Не за графіком",
        "📅 Відключень за графіком більше не заплановано",
    ]


def test_next_outage_tomorrow():
    tomorrow = Outage(local(2026, 10, 16, 11), local(2026, 10, 16, 15))
    match = ScheduleMatch(MatchKind.RESTORED, EVENING, timedelta(), tomorrow)
    lines = texts.schedule_lines(match, local(2026, 10, 16, 0), KYIV)
    assert lines[-1] == "📅 Наступне відключення: сьогодні з 11:00 до 15:00"
    lines = texts.schedule_lines(match, local(2026, 10, 15, 23, 59), KYIV)
    assert lines[-1] == "📅 Наступне відключення: завтра з 11:00 до 15:00"


def test_grid_lost_message_gets_schedule_lines():
    event = Event(
        EventType.GRID_LOST,
        local(2026, 10, 15, 11, 18),
        87,
        previous_duration=timedelta(hours=16, minutes=52),
        match=ScheduleMatch(MatchKind.LOST, MORNING, timedelta(minutes=18)),
    )
    text = texts.event_text(event, KYIV)
    assert text.splitlines() == [
        "🔴 <b>11:18 Світло зникло</b>",
        "🕓 Воно було 16 год 52 хв",
        "🔋 Заряд 87%",
        "⏱ За графіком з 11:00 — на 18 хв пізніше",
        "⏳ Очікуємо світло о 15:00",
    ]


def test_grid_restored_message_keeps_generator_line_before_schedule():
    event = Event(
        EventType.GRID_RESTORED,
        local(2026, 10, 15, 14, 48),
        60,
        previous_duration=timedelta(hours=3),
        gen_was_running=True,
        match=ScheduleMatch(MatchKind.RESTORED, MORNING, timedelta(minutes=-12), None),
    )
    lines = texts.event_text(event, KYIV).splitlines()
    assert lines[2] == "⛽ Генератор можна вимикати"
    assert lines[3:] == ["⏱ За графіком до 15:00 — на 12 хв раніше", "📅 Відключень за графіком більше не заплановано"]


def test_today_digest():
    text = texts.event_text(Event(EventType.SCHEDULE_TODAY, local(2026, 10, 15, 9), schedule=TODAY_SCHEDULE), KYIV)
    assert text.splitlines() == [
        "📅 <b>Графік на сьогодні</b> · четвер, 15 жовтня · черга 3.2",
        "🕐 з 11:00 до 15:00 (~4 год)",
        "❔ можливо з 18:00 до 19:00",
        "🕐 з 20:30 до 24:00 (~3.5 год)",
        "Без світла: 7 год 30 хв",
    ]


def test_tomorrow_digest_without_outages():
    schedule = DaySchedule(GROUP, TOMORROW)
    text = texts.event_text(Event(EventType.SCHEDULE_TOMORROW, local(2026, 10, 15, 21), schedule=schedule), KYIV)
    assert text.splitlines() == [
        "📅 <b>Графік на завтра</b> · пʼятниця, 16 жовтня · черга 3.2",
        "✅ Відключень не заплановано",
    ]


def test_tomorrow_pending_and_published():
    pending = texts.event_text(Event(EventType.SCHEDULE_TOMORROW_PENDING, local(2026, 10, 15, 21)), KYIV)
    assert pending == "⏳ Графік на завтра ще не опубліковано"
    schedule = DaySchedule(GROUP, TOMORROW, (Outage(local(2026, 10, 16, 11), local(2026, 10, 16, 15)),))
    published = texts.event_text(
        Event(EventType.SCHEDULE_TOMORROW_PUBLISHED, local(2026, 10, 15, 22), schedule=schedule), KYIV
    )
    assert published.splitlines()[0] == "📅 <b>Графік на завтра опубліковано</b> · пʼятниця, 16 жовтня · черга 3.2"


@pytest.mark.parametrize(("day", "word"), [(TODAY, "сьогодні"), (TOMORROW, "завтра")])
def test_changed_notice(day, word):
    event = Event(EventType.SCHEDULE_CHANGED, local(2026, 10, 15, 22), schedule=DaySchedule(GROUP, day))
    assert texts.event_text(event, KYIV).splitlines()[0].startswith(f"🔄 <b>Графік на {word} змінився</b>")


def test_source_health_texts_go_to_the_admin():
    down = Event(EventType.SCHEDULE_SOURCE_DOWN, local(2026, 10, 15, 21, 5))
    assert texts.event_text(down, KYIV) == "⚠️ Графік відключень не оновлюється з 21:05"
    assert down.audience == Audience.ADMIN
    up = Event(EventType.SCHEDULE_SOURCE_UP, local(2026, 10, 15, 22))
    assert texts.event_text(up, KYIV) == "✅ Графік відключень знову оновлюється"
    assert up.audience == Audience.ADMIN
    assert Event(EventType.SCHEDULE_TODAY, local(2026, 10, 15, 9)).audience == Audience.SUBSCRIBERS


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (12, 0, "📅 За графіком світла немає до 15:00"),
        (18, 30, "📅 Можливе відключення до 19:00"),
        (16, 0, "📅 Можливе відключення: сьогодні з 18:00 до 19:00"),
        (19, 30, "📅 Наступне відключення: сьогодні з 20:30 до 24:00"),
        (23, 59, "📅 За графіком світла немає до 24:00"),
    ],
)
def test_schedule_status_line(hour, minute, expected):
    assert texts.schedule_status_line(SCHEDULE, local(2026, 10, 15, hour, minute), KYIV) == expected


def test_schedule_status_line_with_nothing_planned():
    schedule = Schedule(GROUP, {TODAY: DaySchedule(GROUP, TODAY)})
    assert texts.schedule_status_line(schedule, local(2026, 10, 15, 12), KYIV) == "📅 Відключень за графіком не заплановано"


def test_status_text_includes_the_schedule_line():
    snapshot = make_snapshot(0)
    state = State(grid=Debounced(True, snapshot.taken_at), gen=Debounced(False, snapshot.taken_at))
    now = local(2026, 10, 15, 12)
    with_schedule = texts.status_text(snapshot, state, now, KYIV, SCHEDULE).splitlines()
    assert "📅 За графіком світла немає до 15:00" in with_schedule
    assert with_schedule.index("📅 За графіком світла немає до 15:00") == with_schedule.index("☀️ Сонце: 0 Вт") + 1
    assert not any(line.startswith("📅") for line in texts.status_text(snapshot, state, now, KYIV).splitlines())
    assert texts.status_text(None, None, now, KYIV, SCHEDULE) == f"{texts.NO_DATA}\n📅 За графіком світла немає до 15:00"


def test_payload_includes_schedule_and_match_only_when_set():
    plain = Event(EventType.GRID_LOST, local(2026, 10, 15, 11), 80, timedelta(hours=1))
    assert set(plain.to_payload()) == {"soc", "previous_duration_s", "gen_was_running"}
    rich = Event(
        EventType.SCHEDULE_TODAY,
        local(2026, 10, 15, 9),
        schedule=TODAY_SCHEDULE,
        match=ScheduleMatch(MatchKind.LOST),
    )
    payload = rich.to_payload()
    assert payload["schedule"] == TODAY_SCHEDULE.to_dict()
    assert payload["match"] == ScheduleMatch(MatchKind.LOST).to_payload()
```

(`make_snapshot(0)` is taken at 07:00 UTC = 10:00 Kyiv, so with `now` = 12:00 Kyiv the snapshot is stale
and `status_text` adds its stale line after "☀️ Сонце". The test checks that the schedule line sits
directly after "☀️ Сонце", before "🕐 Оновлено" and the stale line.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule_texts.py -q`
Expected: FAIL (ImportError / AttributeError on the new names).

- [ ] **Step 3: Extend `lxbot/monitor/detector.py`**

Add the imports:

```python
from lxbot.schedule.compare import ScheduleMatch
from lxbot.schedule.model import DaySchedule
```

Add to `EventType` (after `DONGLE_UP`):

```python
    SCHEDULE_TODAY = "schedule_today"
    SCHEDULE_TOMORROW = "schedule_tomorrow"
    SCHEDULE_TOMORROW_PENDING = "schedule_tomorrow_pending"
    SCHEDULE_TOMORROW_PUBLISHED = "schedule_tomorrow_published"
    SCHEDULE_CHANGED = "schedule_changed"
    SCHEDULE_SOURCE_DOWN = "schedule_source_down"
    SCHEDULE_SOURCE_UP = "schedule_source_up"
```

Add to `_AUDIENCES`:

```python
    EventType.SCHEDULE_SOURCE_DOWN: Audience.ADMIN,
    EventType.SCHEDULE_SOURCE_UP: Audience.ADMIN,
```

Replace `Event` with:

```python
@dataclass(frozen=True)
class Event:
    type: EventType
    at: datetime
    soc: int | None = None
    previous_duration: timedelta | None = None
    gen_was_running: bool = False
    schedule: DaySchedule | None = None
    match: ScheduleMatch | None = None

    @property
    def audience(self) -> Audience:
        return _AUDIENCES.get(self.type, Audience.SUBSCRIBERS)

    def to_payload(self) -> dict:
        duration = self.previous_duration
        payload = {
            "soc": self.soc,
            "previous_duration_s": int(duration.total_seconds()) if duration is not None else None,
            "gen_was_running": self.gen_was_running,
        }
        if self.schedule is not None:
            payload["schedule"] = self.schedule.to_dict()
        if self.match is not None:
            payload["match"] = self.match.to_payload()
        return payload
```

- [ ] **Step 4: Extend `lxbot/bot/texts.py`**

Change the datetime import to `from datetime import date, datetime, time, timedelta, tzinfo`, and add:

```python
from lxbot.schedule.compare import MatchKind, ScheduleMatch, current_outage, next_outage
from lxbot.schedule.model import DaySchedule, Outage, Schedule
```

Add these constants next to the existing ones:

```python
ON_TIME = timedelta(minutes=2)
SCHEDULE_TOMORROW_PENDING = "⏳ Графік на завтра ще не опубліковано"
SCHEDULE_SOURCE_UP = "✅ Графік відключень знову оновлюється"
_WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя")
_MONTHS = (
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
)
```

Add these functions after `_clock`:

```python
# A local-midnight end closes the day it ends, as on DTEK's own schedule: "до 24:00".
def _range_end(outage: Outage, tz: tzinfo) -> str:
    end = outage.end.astimezone(tz)
    if end.time() == time(0) and end.date() > outage.start.astimezone(tz).date():
        return "24:00"
    return f"{end:%H:%M}"


def format_range(outage: Outage, tz: tzinfo) -> str:
    return f"з {_clock(outage.start, tz)} до {_range_end(outage, tz)}"


def _approx_hours(duration: timedelta) -> str:
    half_hours = round(duration.total_seconds() / 1800)
    return f"~{half_hours / 2:g} год"


def format_deviation(deviation: timedelta) -> str:
    if abs(deviation) <= ON_TIME:
        return "вчасно"
    direction = "пізніше" if deviation > timedelta() else "раніше"
    return f"на {format_duration(abs(deviation))} {direction}"


def _day_word(moment: datetime, now: datetime, tz: tzinfo) -> str:
    days = (moment.astimezone(tz).date() - now.astimezone(tz).date()).days
    return {0: "сьогодні", 1: "завтра"}.get(days, f"{moment.astimezone(tz):%d.%m}")


def _upcoming_line(outage: Outage, now: datetime, tz: tzinfo) -> str:
    kind = "Наступне відключення" if outage.certain else "Можливе відключення"
    return f"📅 {kind}: {_day_word(outage.start, now, tz)} {format_range(outage, tz)}"


def schedule_lines(match: ScheduleMatch, at: datetime, tz: tzinfo) -> list[str]:
    interval = match.interval
    if interval is None:
        lines = ["⚠️ Не за графіком"]
    else:
        planned = "За графіком" if interval.certain else "За можливим відключенням"
        deviation = format_deviation(match.deviation)
        if match.kind is MatchKind.LOST:
            lines = [
                f"⏱ {planned} з {_clock(interval.start, tz)} — {deviation}",
                f"⏳ Очікуємо світло о {_clock(interval.end, tz)}",
            ]
        else:
            lines = [f"⏱ {planned} до {_range_end(interval, tz)} — {deviation}"]
    if match.kind is MatchKind.RESTORED:
        if match.next_outage is None:
            lines.append("📅 Відключень за графіком більше не заплановано")
        else:
            lines.append(_upcoming_line(match.next_outage, at, tz))
    return lines


def _date_title(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {day.day} {_MONTHS[day.month - 1]}"


def day_schedule_text(title: str, schedule: DaySchedule, tz: tzinfo, icon: str = "📅") -> str:
    lines = [f"{icon} <b>{title}</b> · {_date_title(schedule.day)} · черга {schedule.label}"]
    if not schedule.outages:
        lines.append("✅ Відключень не заплановано")
        return "\n".join(lines)
    for outage in schedule.outages:
        if outage.certain:
            lines.append(f"🕐 {format_range(outage, tz)} ({_approx_hours(outage.duration)})")
        else:
            lines.append(f"❔ можливо {format_range(outage, tz)}")
    if schedule.certain_total:
        lines.append(f"Без світла: {format_duration(schedule.certain_total)}")
    return "\n".join(lines)


def schedule_status_line(schedule: Schedule, now: datetime, tz: tzinfo) -> str:
    timeline = schedule.timeline()
    current = current_outage(timeline, now)
    if current is not None:
        prefix = "📅 За графіком світла немає до " if current.certain else "📅 Можливе відключення до "
        return prefix + _range_end(current, tz)
    upcoming = next_outage(timeline, now)
    if upcoming is None:
        return "📅 Відключень за графіком не заплановано"
    return _upcoming_line(upcoming, now, tz)
```

In `event_text`:
- Rename the local `time = _clock(event.at, tz)` to `clock = _clock(event.at, tz)` and use `clock`
  everywhere in that function (the module now imports `datetime.time`).
- Make the GRID_LOST and GRID_RESTORED cases append the schedule lines.
- Add the schedule cases.

The whole function becomes:

```python
def event_text(event: Event, tz: tzinfo) -> str | None:
    clock = _clock(event.at, tz)
    match event.type:
        case EventType.GRID_LOST:
            text = (
                f"🔴 <b>{clock} Світло зникло</b>\n"
                f"🕓 Воно було {format_duration(event.previous_duration)}\n"
                f"🔋 Заряд {event.soc}%"
            )
            return _with_schedule(text, event, tz)
        case EventType.GRID_RESTORED:
            text = f"🟢 <b>{clock} Світло з'явилось</b>\n🕓 Його не було {format_duration(event.previous_duration)}"
            if event.gen_was_running:
                text += "\n⛽ Генератор можна вимикати"
            return _with_schedule(text, event, tz)
        case EventType.BATTERY_LOW:
            return f"🪫 <b>Залишилось {event.soc}%</b>\n⛽ Час заводити генератор"
        case EventType.BATTERY_CRITICAL:
            return f"🚨 <b>Залишилось {event.soc}%</b>\n⛽ Терміново заведіть генератор"
        case EventType.GEN_CHARGED:
            return f"🔋 <b>Заряджено {event.soc}%</b>\n⛽ Генератор можна вимикати"
        case EventType.DONGLE_DOWN:
            return f"⚠️ Немає зв'язку з інвертором з {clock}"
        case EventType.DONGLE_UP:
            return f"✅ Зв'язок з інвертором відновлено о {clock}"
        case EventType.SCHEDULE_TODAY:
            return day_schedule_text("Графік на сьогодні", event.schedule, tz)
        case EventType.SCHEDULE_TOMORROW:
            return day_schedule_text("Графік на завтра", event.schedule, tz)
        case EventType.SCHEDULE_TOMORROW_PUBLISHED:
            return day_schedule_text("Графік на завтра опубліковано", event.schedule, tz)
        case EventType.SCHEDULE_TOMORROW_PENDING:
            return SCHEDULE_TOMORROW_PENDING
        case EventType.SCHEDULE_CHANGED:
            word = _day_word(datetime.combine(event.schedule.day, time(12), tz), event.at, tz)
            return day_schedule_text(f"Графік на {word} змінився", event.schedule, tz, icon="🔄")
        case EventType.SCHEDULE_SOURCE_DOWN:
            return f"⚠️ Графік відключень не оновлюється з {clock}"
        case EventType.SCHEDULE_SOURCE_UP:
            return SCHEDULE_SOURCE_UP
        case _:
            return None


def _with_schedule(text: str, event: Event, tz: tzinfo) -> str:
    if event.match is None:
        return text
    return "\n".join([text, *schedule_lines(event.match, event.at, tz)])
```

Change `status_text` to take the schedule and put its line right after "☀️ Сонце":

```python
def status_text(
    snapshot: Snapshot | None, state: State | None, now: datetime, tz: tzinfo, schedule: Schedule | None = None
) -> str:
    schedule_line = schedule_status_line(schedule, now, tz) if schedule is not None else None
    if snapshot is None or state is None:
        return f"{NO_DATA}\n{schedule_line}" if schedule_line else NO_DATA
    # ... existing body up to and including the "☀️ Сонце" line, unchanged ...
    lines = [
        f"⚡ Мережа: {grid}",
        f"⛽ Генератор: {'працює' if state.gen.value else 'не працює'}",
        f"🔋 Батарея: {snapshot.soc}%{flow}",
        f"🏠 Споживання: {format_power(snapshot.load_w)}",
        f"☀️ Сонце: {format_power(snapshot.pv_w)}",
    ]
    if schedule_line:
        lines.append(schedule_line)
    lines.append(f"🕐 Оновлено {snapshot.taken_at.astimezone(tz):%H:%M:%S}")
    if now - snapshot.taken_at > STALE_AFTER:
        lines.append("⚠️ Дані застарілі: немає зв'язку з інвертором")
    return "\n".join(lines)
```

Keep the existing grid / "(перевіряю)" / battery-flow logic between the two parts exactly as it is today.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule_texts.py tests/test_texts.py tests/test_detector.py -q -W error`
Expected: all pass. Then the full suite, all pass.

- [ ] **Step 6: Commit**

```bash
git add lxbot/monitor/detector.py lxbot/bot/texts.py tests/test_schedule_texts.py
git commit -m "Add schedule events and their Ukrainian texts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: HTTP schedule source

**Files:**
- Create: `lxbot/schedule/source.py`
- Modify: `requirements.txt` (add `aiohttp==3.14.5`)
- Test: `tests/test_schedule_source.py`

**Interfaces:**
- Produces (`lxbot.schedule.source`):
  - `class ScheduleSourceError(Exception)`;
  - `class ScheduleSource(Protocol)` with `async fetch() -> dict | None` (None means unchanged since the
    previous fetch) and `async close() -> None`;
  - `class HttpScheduleSource(url: str, timeout: float = 20.0)` implementing it with `If-None-Match`.

- [ ] **Step 1: Pin aiohttp**

`requirements.txt` becomes:

```
aiogram==3.31.0
aiohttp==3.14.5
aiosqlite==0.22.1
tzdata==2026.5
```

Check: `.venv/bin/pip show aiohttp` reports 3.14.5 (it is already installed by aiogram).

- [ ] **Step 2: Write the failing tests**

`tests/test_schedule_source.py`:

```python
import json

import pytest
from aiohttp import test_utils, web

from lxbot.schedule.source import HttpScheduleSource, ScheduleSourceError


class Remote:
    def __init__(self):
        self.status = 200
        self.body = json.dumps({"fact": {"data": {}}})
        self.etag: str | None = '"v1"'
        self.seen_etags: list[str | None] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.seen_etags.append(request.headers.get("If-None-Match"))
        if self.etag and request.headers.get("If-None-Match") == self.etag:
            return web.Response(status=304)
        headers = {"ETag": self.etag} if self.etag else {}
        return web.Response(status=self.status, text=self.body, content_type="text/plain", headers=headers)


@pytest.fixture
async def remote():
    state = Remote()
    app = web.Application()
    app.router.add_get("/kyiv-region.json", state.handle)
    # Via the module: a bare `TestServer` name in a test module is collected by pytest as a test class.
    server = test_utils.TestServer(app)
    await server.start_server()
    state.url = str(server.make_url("/kyiv-region.json"))
    yield state
    await server.close()


@pytest.fixture
async def source(remote):
    source = HttpScheduleSource(remote.url, timeout=2)
    yield source
    await source.close()


async def test_fetch_returns_the_document(source):
    assert await source.fetch() == {"fact": {"data": {}}}


async def test_unchanged_document_returns_none(remote, source):
    await source.fetch()
    assert await source.fetch() is None
    assert remote.seen_etags == [None, '"v1"']


async def test_without_etag_every_fetch_returns_the_document(remote, source):
    remote.etag = None
    assert await source.fetch() is not None
    assert await source.fetch() is not None


async def test_http_error(remote, source):
    remote.status = 500
    with pytest.raises(ScheduleSourceError, match="HTTP 500"):
        await source.fetch()


async def test_invalid_json(remote, source):
    remote.body = "<html>oops</html>"
    with pytest.raises(ScheduleSourceError):
        await source.fetch()


async def test_json_that_is_not_an_object(remote, source):
    remote.body = "[1, 2]"
    with pytest.raises(ScheduleSourceError, match="not a JSON object"):
        await source.fetch()


async def test_unreachable_source():
    source = HttpScheduleSource("http://127.0.0.1:9/kyiv-region.json", timeout=2)
    with pytest.raises(ScheduleSourceError):
        await source.fetch()
    await source.close()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule_source.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.schedule.source'`

- [ ] **Step 4: Implement `lxbot/schedule/source.py`**

```python
from http import HTTPStatus
from typing import Protocol

import aiohttp


class ScheduleSourceError(Exception):
    pass


class ScheduleSource(Protocol):
    async def fetch(self) -> dict | None: ...

    async def close(self) -> None: ...


class HttpScheduleSource:
    def __init__(self, url: str, timeout: float = 20.0):
        self._url = url
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._etag: str | None = None

    async def fetch(self) -> dict | None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        headers = {"If-None-Match": self._etag} if self._etag else {}
        try:
            async with self._session.get(self._url, headers=headers) as response:
                if response.status == HTTPStatus.NOT_MODIFIED:
                    return None
                if response.status != HTTPStatus.OK:
                    raise ScheduleSourceError(f"HTTP {response.status} from {self._url}")
                # raw.githubusercontent.com serves JSON as text/plain.
                document = await response.json(content_type=None)
                etag = response.headers.get("ETag")
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ScheduleSourceError(f"fetching {self._url}: {exc!r}") from exc
        if not isinstance(document, dict):
            raise ScheduleSourceError("the schedule document is not a JSON object")
        self._etag = etag
        return document

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule_source.py -q -W error`
Expected: all pass (7), with no "Unclosed client session" warnings. Then the full suite, all pass.

- [ ] **Step 6: Commit**

```bash
git add lxbot/schedule/source.py requirements.txt tests/test_schedule_source.py
git commit -m "Fetch the outage schedule over HTTP with ETag

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Schedule service

**Files:**
- Create: `lxbot/schedule/service.py`
- Test: `tests/test_schedule_service.py`

**Interfaces:**
- Consumes:
  - `Schedule`, `DaySchedule` (Task 1);
  - `parse_schedule`, `ScheduleParseError` (Task 1);
  - `ScheduleSource`, `ScheduleSourceError` (Task 4);
  - `Event`, `EventType` (Task 3);
  - `EventSink` (`lxbot.monitor.poller`);
  - `Database` (`load_state` / `save_state`).
- Produces (`lxbot.schedule.service`):
  - `DEFAULT_URL`;
  - frozen dataclass `ScheduleConfig(group: str, url: str = DEFAULT_URL,
    poll_interval: timedelta = timedelta(minutes=15), match_window: timedelta = timedelta(minutes=30),
    today_at: time = time(9), tomorrow_at: time = time(21))`;
  - `class ScheduleService(source, db, sink, config, tz, *, clock=<utc now>, tick_seconds: float = 60.0)`
    with:
    - `config: ScheduleConfig` (property);
    - `current(now) -> Schedule | None` (None when unknown or fetched more than 6 h ago);
    - `async load()`, `async tick(now)` and `async run()` (load is NOT called by run; main calls it).

- [ ] **Step 1: Write the failing tests**

`tests/test_schedule_service.py`:

```python
from datetime import UTC, datetime, timedelta

from lxbot.monitor.detector import EventType
from lxbot.schedule.model import Outage
from lxbot.schedule.service import ScheduleConfig, ScheduleService
from lxbot.schedule.source import ScheduleSourceError
from tests.schedule_factories import GROUP, KYIV, TODAY, TOMORROW, local, make_document

CONFIG = ScheduleConfig(group=GROUP)
TODAY_OFF = {15: "no", 16: "no", 17: "no", 18: "no"}
TOMORROW_OFF = {12: "no", 13: "no", 14: "no", 15: "no"}
DOC = make_document({TODAY: TODAY_OFF, TOMORROW: TOMORROW_OFF})
DOC_TODAY_ONLY = make_document({TODAY: TODAY_OFF})
DOC_TODAY_CHANGED = make_document({TODAY: {16: "no", 17: "no", 18: "no", 19: "no"}, TOMORROW: TOMORROW_OFF})
DOC_TOMORROW_CHANGED = make_document({TODAY: TODAY_OFF, TOMORROW: {13: "no"}})
DOC_TOMORROW_EMPTY = make_document({TODAY: TODAY_OFF, TOMORROW: {}})


class FakeSource:
    """Returns the scripted responses in order; the last one repeats."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


class RecordingSink:
    def __init__(self):
        self.events = []

    async def publish(self, events):
        self.events.extend(events)


async def make_service(db, *responses):
    sink = RecordingSink()
    source = FakeSource(*responses)
    service = ScheduleService(source, db, sink, CONFIG, KYIV)
    await service.load()
    return service, sink, source


def types(sink):
    return [e.type for e in sink.events]


def at(hour, minute=0, day=15):
    return local(2026, 10, day, hour, minute)


async def test_morning_digest_once_at_nine(db):
    service, sink, _ = await make_service(db, DOC)
    await service.tick(at(8, 59))
    assert sink.events == []
    await service.tick(at(9, 0))
    await service.tick(at(9, 1))
    assert types(sink) == [EventType.SCHEDULE_TODAY]
    digest = sink.events[0]
    assert digest.at == at(9, 0)
    assert digest.schedule.outages == (Outage(at(14), at(18)),)


async def test_late_start_still_sends_the_morning_digest(db):
    service, sink, _ = await make_service(db, DOC)
    await service.tick(at(11, 0))
    assert types(sink) == [EventType.SCHEDULE_TODAY]


async def test_no_resend_after_restart(db):
    first, sink, _ = await make_service(db, DOC)
    await first.tick(at(9, 0))
    second, second_sink, _ = await make_service(db, DOC)
    await second.tick(at(9, 5))
    assert types(sink) == [EventType.SCHEDULE_TODAY]
    assert second_sink.events == []


async def test_evening_digest_for_tomorrow(db):
    service, sink, _ = await make_service(db, DOC)
    await service.tick(at(21, 0))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW]
    assert sink.events[0].schedule.day == TOMORROW


async def test_tomorrow_pending_then_published(db):
    service, sink, _ = await make_service(db, DOC_TODAY_ONLY, DOC)
    await service.tick(at(21, 0))
    await service.tick(at(21, 15))
    await service.tick(at(21, 30))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW_PENDING, EventType.SCHEDULE_TOMORROW_PUBLISHED]
    assert sink.events[1].schedule.day == TOMORROW


async def test_published_after_midnight_waits_for_the_morning_digest(db):
    service, sink, _ = await make_service(db, DOC_TODAY_ONLY, make_document({TOMORROW: TOMORROW_OFF}))
    await service.tick(at(21, 0))
    await service.tick(at(0, 10, day=16))
    await service.tick(at(9, 0, day=16))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW_PENDING, EventType.SCHEDULE_TODAY]
    assert sink.events[1].schedule.day == TOMORROW


async def test_change_after_the_morning_digest_is_announced(db):
    service, sink, _ = await make_service(db, DOC, DOC_TODAY_CHANGED)
    await service.tick(at(9, 0))
    await service.tick(at(9, 15))
    assert types(sink) == [EventType.SCHEDULE_TODAY, EventType.SCHEDULE_CHANGED]
    assert sink.events[1].schedule.outages == (Outage(at(15), at(19)),)


async def test_change_before_the_digest_is_silent_and_the_digest_has_it(db):
    service, sink, _ = await make_service(db, DOC, DOC_TODAY_CHANGED)
    await service.tick(at(8, 30))
    await service.tick(at(8, 45))
    await service.tick(at(9, 0))
    assert types(sink) == [EventType.SCHEDULE_TODAY]
    assert sink.events[0].schedule.outages == (Outage(at(15), at(19)),)


async def test_tomorrow_change_after_the_evening_digest_is_announced(db):
    service, sink, _ = await make_service(db, DOC, DOC_TOMORROW_CHANGED)
    await service.tick(at(21, 0))
    await service.tick(at(21, 15))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW, EventType.SCHEDULE_CHANGED]
    assert sink.events[1].schedule.day == TOMORROW


async def test_tomorrow_filled_after_the_evening_digest_is_announced(db):
    service, sink, _ = await make_service(db, DOC_TOMORROW_EMPTY, DOC)
    await service.tick(at(21, 0))
    await service.tick(at(21, 15))
    assert types(sink) == [EventType.SCHEDULE_TOMORROW, EventType.SCHEDULE_CHANGED]
    assert sink.events[0].schedule.outages == ()


async def test_recent_fetch_is_reused_for_the_digest(db):
    service, sink, source = await make_service(db, DOC)
    await service.tick(at(8, 50))
    await service.tick(at(9, 0))
    assert source.calls == 1
    assert types(sink) == [EventType.SCHEDULE_TODAY]


async def test_digest_refreshes_an_old_schedule_first(db):
    # 08:50 fails, so at 09:00 the last good fetch is an hour old although an attempt was 10 min ago.
    service, sink, source = await make_service(db, DOC, ScheduleSourceError("blip"), DOC_TODAY_CHANGED)
    await service.tick(at(8, 0))
    await service.tick(at(8, 50))
    await service.tick(at(9, 0))
    assert source.calls == 3
    assert sink.events[-1].type == EventType.SCHEDULE_TODAY
    assert sink.events[-1].schedule.outages == (Outage(at(15), at(19)),)


async def test_not_modified_keeps_the_schedule_fresh(db):
    service, _, _ = await make_service(db, DOC, None)
    await service.tick(at(9, 0))
    await service.tick(at(14, 0))
    assert service.current(at(19, 30)) is not None
    assert service.current(at(19, 30)).day(TODAY).outages == (Outage(at(14), at(18)),)


async def test_refresh_failure_keeps_the_last_schedule_until_it_is_stale(db):
    service, _, _ = await make_service(db, DOC, ScheduleSourceError("down"))
    await service.tick(at(9, 0))
    await service.tick(at(9, 15))
    assert service.current(at(9, 16)) is not None
    assert service.current(at(15, 0)) is not None
    assert service.current(at(15, 1)) is None


async def test_schedule_survives_a_restart_without_network(db):
    first, _, _ = await make_service(db, DOC)
    await first.tick(at(8, 30))
    second, sink, _ = await make_service(db, ScheduleSourceError("down"))
    assert second.current(at(8, 40)).day(TODAY) == first.current(at(8, 40)).day(TODAY)
    await second.tick(at(9, 0))
    assert types(sink) == [EventType.SCHEDULE_TODAY]


async def test_no_schedule_no_digest(db):
    service, sink, _ = await make_service(db, ScheduleSourceError("down"))
    await service.tick(at(9, 0))
    assert sink.events == []
    assert service.current(at(9, 0)) is None


async def test_source_down_after_an_hour_then_up(db):
    # With no schedule and a digest due, every tick retries; the third failure is at 10:00.
    down = ScheduleSourceError("down")
    service, sink, _ = await make_service(db, down, down, down, DOC)
    await service.tick(at(9, 0))
    await service.tick(at(9, 59))
    assert sink.events == []
    await service.tick(at(10, 0))
    assert types(sink) == [EventType.SCHEDULE_SOURCE_DOWN]
    assert sink.events[0].at == at(9, 0)
    await service.tick(at(10, 15))
    assert types(sink)[1:] == [EventType.SCHEDULE_SOURCE_UP, EventType.SCHEDULE_TODAY]


async def test_source_reporting_not_ok_counts_as_down(db):
    service, sink, _ = await make_service(db, make_document({TODAY: TODAY_OFF}, ok=False))
    await service.tick(at(9, 0))
    await service.tick(at(10, 0))
    assert EventType.SCHEDULE_SOURCE_DOWN in types(sink)


async def test_stale_source_counts_as_down(db):
    old = make_document({TODAY: TODAY_OFF}, last_updated=datetime(2026, 10, 14, 0, 0, tzinfo=UTC))
    service, sink, _ = await make_service(db, old)
    await service.tick(at(9, 0))
    await service.tick(at(10, 0))
    assert EventType.SCHEDULE_SOURCE_DOWN in types(sink)


async def test_run_survives_an_unexpected_error(db):
    class BrokenOnce:
        calls = 0

        async def fetch(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bug")
            return DOC

        async def close(self):
            pass

    times = iter([at(9, 0), at(9, 15), at(9, 30)])

    def clock():
        moment = next(times, None)
        if moment is None:
            raise SystemExit
        return moment

    sink = RecordingSink()
    service = ScheduleService(BrokenOnce(), db, sink, CONFIG, KYIV, clock=clock, tick_seconds=0)
    try:
        await service.run()
    except SystemExit:
        pass
    assert EventType.SCHEDULE_TODAY in types(sink)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule_service.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.schedule.service'`

- [ ] **Step 3: Implement `lxbot/schedule/service.py`**

```python
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from lxbot.monitor.detector import Event, EventType
from lxbot.monitor.poller import EventSink
from lxbot.schedule.model import DaySchedule, Schedule
from lxbot.schedule.parse import ScheduleParseError, parse_schedule
from lxbot.schedule.source import ScheduleSource, ScheduleSourceError
from lxbot.storage.db import Database

log = logging.getLogger(__name__)

DEFAULT_URL = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json"
SCHEDULE_KEY = "schedule"
DIGESTS_KEY = "schedule-digests"
TICK_SECONDS = 60.0
STALE_AFTER = timedelta(hours=6)
REFRESH_BEFORE_DIGEST = timedelta(minutes=15)
SOURCE_MAX_AGE = timedelta(hours=1)
SOURCE_DOWN_AFTER = timedelta(hours=1)


@dataclass(frozen=True)
class ScheduleConfig:
    group: str
    url: str = DEFAULT_URL
    poll_interval: timedelta = timedelta(minutes=15)
    match_window: timedelta = timedelta(minutes=30)
    today_at: time = time(9)
    tomorrow_at: time = time(21)


@dataclass
class _Digests:
    today: date | None = None
    tomorrow: date | None = None
    pending: date | None = None

    def to_dict(self) -> dict:
        return {name: value.isoformat() if value else None for name, value in vars(self).items()}

    @classmethod
    def from_dict(cls, data: dict) -> "_Digests":
        return cls(**{name: date.fromisoformat(value) if value else None for name, value in data.items()})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _optional_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class ScheduleService:
    def __init__(
        self,
        source: ScheduleSource,
        db: Database,
        sink: EventSink,
        config: ScheduleConfig,
        tz: ZoneInfo,
        *,
        clock: Callable[[], datetime] = _utc_now,
        tick_seconds: float = TICK_SECONDS,
    ):
        self._source = source
        self._db = db
        self._sink = sink
        self._config = config
        self._tz = tz
        self._clock = clock
        self._tick_seconds = tick_seconds
        self._schedule: Schedule | None = None
        self._fetched_at: datetime | None = None
        self._last_attempt: datetime | None = None
        self._fetch_ok = True
        self._digests = _Digests()
        self._failing_since: datetime | None = None
        self._down_reported = False

    @property
    def config(self) -> ScheduleConfig:
        return self._config

    def current(self, now: datetime) -> Schedule | None:
        if self._schedule is None or self._fetched_at is None or now - self._fetched_at > STALE_AFTER:
            return None
        return self._schedule

    async def load(self) -> None:
        stored = await self._db.load_state(SCHEDULE_KEY)
        if stored:
            self._schedule = Schedule(
                group=stored["group"],
                days={d.day: d for d in map(DaySchedule.from_dict, stored["days"])},
                dtek_updated=_optional_time(stored["dtek_updated"]),
                source_ok=stored["source_ok"],
                source_updated=_optional_time(stored["source_updated"]),
            )
            self._fetched_at = datetime.fromisoformat(stored["fetched_at"])
        digests = await self._db.load_state(DIGESTS_KEY)
        if digests:
            self._digests = _Digests.from_dict(digests)

    async def run(self) -> None:
        while True:
            try:
                await self.tick(self._clock())
            except Exception:
                log.exception("schedule tick failed")
            await asyncio.sleep(self._tick_seconds)

    async def tick(self, now: datetime) -> None:
        events: list[Event] = []
        if self._refresh_due(now):
            events += await self._refresh(now)
        events += self._health_events(now)
        events += await self._digest_events(now)
        if events:
            await self._sink.publish(events)

    def _local(self, now: datetime) -> datetime:
        return now.astimezone(self._tz)

    def _refresh_due(self, now: datetime) -> bool:
        if self._last_attempt is None or now - self._last_attempt >= self._config.poll_interval:
            return True
        # A digest goes out from the freshest data, not from a fetch up to 15 min old.
        outdated = self._fetched_at is None or now - self._fetched_at > REFRESH_BEFORE_DIGEST
        return outdated and self._digest_due(now)

    async def _refresh(self, now: datetime) -> list[Event]:
        self._last_attempt = now
        try:
            document = await self._source.fetch()
            fresh = None if document is None else parse_schedule(document, self._config.group, self._tz)
        except (ScheduleSourceError, ScheduleParseError) as exc:
            log.warning("schedule refresh failed: %s", exc)
            self._fetch_ok = False
            return []
        self._fetch_ok = True
        self._fetched_at = now
        events: list[Event] = []
        if fresh is not None:
            events = self._change_events(fresh, now)
            self._schedule = fresh
        await self._save()
        return events

    def _change_events(self, fresh: Schedule, now: datetime) -> list[Event]:
        today = self._local(now).date()
        tomorrow = today + timedelta(days=1)
        events = []
        for day, schedule in sorted(fresh.days.items()):
            previous = self._schedule.day(day) if self._schedule else None
            if previous is None:
                if day == tomorrow and self._digests.pending == tomorrow:
                    self._digests.pending = None
                    events.append(Event(EventType.SCHEDULE_TOMORROW_PUBLISHED, now, schedule=schedule))
                continue
            if previous.outages == schedule.outages:
                continue
            announced = (day == today and self._digests.today == today) or (
                day == tomorrow and self._digests.tomorrow == tomorrow and self._digests.pending != tomorrow
            )
            if announced:
                events.append(Event(EventType.SCHEDULE_CHANGED, now, schedule=schedule))
        return events

    def _healthy(self, now: datetime) -> bool:
        if not self._fetch_ok or self._schedule is None or not self._schedule.source_ok:
            return False
        updated = self._schedule.source_updated
        return updated is None or now - updated <= SOURCE_MAX_AGE

    def _health_events(self, now: datetime) -> list[Event]:
        if self._healthy(now):
            self._failing_since = None
            if not self._down_reported:
                return []
            self._down_reported = False
            return [Event(EventType.SCHEDULE_SOURCE_UP, now)]
        if self._failing_since is None:
            self._failing_since = now
        if self._down_reported or now - self._failing_since < SOURCE_DOWN_AFTER:
            return []
        self._down_reported = True
        return [Event(EventType.SCHEDULE_SOURCE_DOWN, self._failing_since)]

    def _digest_due(self, now: datetime) -> bool:
        local = self._local(now)
        today = local.date()
        if self._config.today_at <= local.time() < self._config.tomorrow_at:
            return self._digests.today != today
        if local.time() >= self._config.tomorrow_at:
            return self._digests.tomorrow != today + timedelta(days=1)
        return False

    async def _digest_events(self, now: datetime) -> list[Event]:
        if self._schedule is None or not self._digest_due(now):
            return []
        local = self._local(now)
        today = local.date()
        if local.time() < self._config.tomorrow_at:
            schedule = self._schedule.day(today)
            if schedule is None:
                return []
            self._digests.today = today
            event = Event(EventType.SCHEDULE_TODAY, now, schedule=schedule)
        else:
            tomorrow = today + timedelta(days=1)
            schedule = self._schedule.day(tomorrow)
            self._digests.tomorrow = tomorrow
            if schedule is None:
                self._digests.pending = tomorrow
                event = Event(EventType.SCHEDULE_TOMORROW_PENDING, now)
            else:
                self._digests.pending = None
                event = Event(EventType.SCHEDULE_TOMORROW, now, schedule=schedule)
        await self._db.save_state(DIGESTS_KEY, self._digests.to_dict())
        return [event]

    async def _save(self) -> None:
        if self._schedule is not None:
            schedule = self._schedule
            await self._db.save_state(
                SCHEDULE_KEY,
                {
                    "group": schedule.group,
                    "days": [d.to_dict() for d in schedule.days.values()],
                    "dtek_updated": schedule.dtek_updated.isoformat() if schedule.dtek_updated else None,
                    "source_ok": schedule.source_ok,
                    "source_updated": schedule.source_updated.isoformat() if schedule.source_updated else None,
                    "fetched_at": self._fetched_at.isoformat(),
                },
            )
        await self._db.save_state(DIGESTS_KEY, self._digests.to_dict())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule_service.py -q -W error`
Expected: all pass. Then the full suite, all pass.

If a test fails because of a rule interaction the plan did not foresee, keep the spec's behaviour:
- digests at 09:00 and 21:00;
- a late morning digest until 21:00;
- no resend after a restart;
- a pending day published only before midnight;
- change notices only after that day's digest.

Fix the smallest thing, and record the deviation in the report.

- [ ] **Step 5: Commit**

```bash
git add lxbot/schedule/service.py tests/test_schedule_service.py
git commit -m "Poll the outage schedule and send daily digests and change notices

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Annotating grid events

**Files:**
- Create: `lxbot/schedule/annotate.py`
- Test: `tests/test_schedule_annotate.py`

**Interfaces:**
- Consumes: `Event`, `EventType` (Task 3); `match_lost`, `match_restored`, `MatchKind` (Task 2);
  `Schedule`, `DaySchedule`, `Outage` (Task 1); `EventSink` (`lxbot.monitor.poller`).
- Produces (`lxbot.schedule.annotate`):
  - `class ScheduleProvider(Protocol)` with `current(now: datetime) -> Schedule | None`;
  - `class ScheduleAnnotator(sink: EventSink, schedules: ScheduleProvider, window: timedelta)` with
    `async publish(events) -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_schedule_annotate.py`:

```python
from datetime import timedelta

from lxbot.monitor.detector import Event, EventType
from lxbot.schedule.annotate import ScheduleAnnotator
from lxbot.schedule.compare import MatchKind, ScheduleMatch
from lxbot.schedule.model import DaySchedule, Outage, Schedule
from tests.schedule_factories import GROUP, TODAY, local

MORNING = Outage(local(2026, 10, 15, 11), local(2026, 10, 15, 15))
EVENING = Outage(local(2026, 10, 15, 20, 30), local(2026, 10, 16, 0))
SCHEDULE = Schedule(GROUP, {TODAY: DaySchedule(GROUP, TODAY, (MORNING, EVENING))})
WINDOW = timedelta(minutes=30)


class Provider:
    def __init__(self, schedule):
        self.schedule = schedule
        self.asked = []

    def current(self, now):
        self.asked.append(now)
        return self.schedule


class RecordingSink:
    def __init__(self):
        self.events = []

    async def publish(self, events):
        self.events.extend(events)


async def publish(schedule, *events):
    sink = RecordingSink()
    provider = Provider(schedule)
    await ScheduleAnnotator(sink, provider, WINDOW).publish(list(events))
    return sink.events, provider


async def test_grid_lost_gets_a_match_for_its_own_time():
    lost = Event(EventType.GRID_LOST, local(2026, 10, 15, 11, 18), 80, timedelta(hours=16))
    (annotated,), provider = await publish(SCHEDULE, lost)
    assert annotated.match == ScheduleMatch(MatchKind.LOST, MORNING, timedelta(minutes=18))
    assert provider.asked == [lost.at]
    assert annotated.soc == 80


async def test_grid_restored_gets_the_next_outage():
    restored = Event(EventType.GRID_RESTORED, local(2026, 10, 15, 14, 48), 60, timedelta(hours=3))
    (annotated,), _ = await publish(SCHEDULE, restored)
    assert annotated.match == ScheduleMatch(MatchKind.RESTORED, MORNING, timedelta(minutes=-12), EVENING)


async def test_other_events_pass_through_untouched():
    low = Event(EventType.BATTERY_LOW, local(2026, 10, 15, 12), 30)
    events, provider = await publish(SCHEDULE, low)
    assert events == [low]
    assert provider.asked == []


async def test_unknown_schedule_leaves_grid_events_as_they_are():
    lost = Event(EventType.GRID_LOST, local(2026, 10, 15, 11, 18), 80, timedelta(hours=1))
    events, _ = await publish(None, lost)
    assert events == [lost]


async def test_order_is_kept():
    lost = Event(EventType.GRID_LOST, local(2026, 10, 15, 11, 18), 25, timedelta(hours=1))
    low = Event(EventType.BATTERY_LOW, local(2026, 10, 15, 11, 19), 25)
    events, _ = await publish(SCHEDULE, lost, low)
    assert [e.type for e in events] == [EventType.GRID_LOST, EventType.BATTERY_LOW]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule_annotate.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'lxbot.schedule.annotate'`

- [ ] **Step 3: Implement `lxbot/schedule/annotate.py`**

```python
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

from lxbot.monitor.detector import Event, EventType
from lxbot.monitor.poller import EventSink
from lxbot.schedule.compare import match_lost, match_restored
from lxbot.schedule.model import Schedule

_MATCHERS = {EventType.GRID_LOST: match_lost, EventType.GRID_RESTORED: match_restored}


class ScheduleProvider(Protocol):
    def current(self, now: datetime) -> Schedule | None: ...


class ScheduleAnnotator:
    def __init__(self, sink: EventSink, schedules: ScheduleProvider, window: timedelta):
        self._sink = sink
        self._schedules = schedules
        self._window = window

    async def publish(self, events: Sequence[Event]) -> None:
        await self._sink.publish([self._annotate(event) for event in events])

    def _annotate(self, event: Event) -> Event:
        matcher = _MATCHERS.get(event.type)
        if matcher is None:
            return event
        schedule = self._schedules.current(event.at)
        if schedule is None:
            return event
        return replace(event, match=matcher(schedule.timeline(), event.at, self._window))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule_annotate.py -q -W error`
Expected: all pass (5). Then the full suite, all pass.

- [ ] **Step 5: Commit**

```bash
git add lxbot/schedule/annotate.py tests/test_schedule_annotate.py
git commit -m "Attach the schedule match to grid events

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Configuration, /status and wiring

**Files:**
- Modify: `lxbot/config.py`, `lxbot/bot/handlers.py`, `lxbot/main.py`, `.env.example`, `README.md`
- Test: `tests/test_config.py`, `tests/test_handlers.py` and `tests/test_main.py` (add tests; existing
  ones keep passing)

**Interfaces:**
- Consumes: `ScheduleConfig`, `ScheduleService`, `DEFAULT_URL` (Task 5); `HttpScheduleSource` (Task 4);
  `ScheduleAnnotator` (Task 6); `group_key` (Task 1); `status_text(..., schedule)` (Task 3).
- Produces:
  - `Settings.schedule: ScheduleConfig | None = None` (last field, so `make_settings` keeps working);
  - `schedule_config_from_env(env) -> ScheduleConfig | None`;
  - `on_status(message, db, settings, poller, schedules=None)`;
  - `stop_background(poller_task, notifier_task, notifier, drain_seconds=..., *, others=())`;
  - `main.run` wires the schedule when `settings.schedule` is set.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
from datetime import time, timedelta

from lxbot.schedule.service import DEFAULT_URL


def test_schedule_is_off_without_a_group():
    assert settings_from_env(ENV).schedule is None
    assert settings_from_env(ENV | {"SCHEDULE_GROUP": "  "}).schedule is None


def test_schedule_defaults():
    schedule = settings_from_env(ENV | {"SCHEDULE_GROUP": "3.2"}).schedule
    assert schedule.group == "GPV3.2"
    assert schedule.url == DEFAULT_URL
    assert schedule.poll_interval == timedelta(minutes=15)
    assert schedule.match_window == timedelta(minutes=30)
    assert (schedule.today_at, schedule.tomorrow_at) == (time(9), time(21))


def test_schedule_overrides():
    env = ENV | {
        "SCHEDULE_GROUP": "6.2",
        "SCHEDULE_URL": "https://example.test/x.json",
        "SCHEDULE_POLL_INTERVAL": "600",
        "SCHEDULE_MATCH_WINDOW": "20",
        "DIGEST_TODAY_AT": "08:30",
        "DIGEST_TOMORROW_AT": "20:00",
    }
    schedule = settings_from_env(env).schedule
    assert schedule.group == "GPV6.2"
    assert schedule.url == "https://example.test/x.json"
    assert schedule.poll_interval == timedelta(minutes=10)
    assert schedule.match_window == timedelta(minutes=20)
    assert (schedule.today_at, schedule.tomorrow_at) == (time(8, 30), time(20))


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DIGEST_TODAY_AT", "9am", "DIGEST_TODAY_AT"),
        ("DIGEST_TOMORROW_AT", "25:00", "DIGEST_TOMORROW_AT"),
        ("SCHEDULE_MATCH_WINDOW", "half", "SCHEDULE_MATCH_WINDOW"),
        ("SCHEDULE_POLL_INTERVAL", "often", "SCHEDULE_POLL_INTERVAL"),
        ("DIGEST_TODAY_AT", "22:00", "earlier than DIGEST_TOMORROW_AT"),
    ],
)
def test_bad_schedule_values(name, value, message):
    with pytest.raises(ConfigError, match=message):
        settings_from_env(ENV | {"SCHEDULE_GROUP": "3.2", name: value})
```

(If `tests/test_config.py` already imports `pytest`, `ConfigError`, `settings_from_env` and `ENV`, reuse
them; put the new imports at the top of the file.)

Append to `tests/test_handlers.py`:

```python
async def test_status_shows_the_next_planned_outage(db):
    from datetime import UTC, datetime, timedelta

    from lxbot.schedule.model import DaySchedule, Outage, Schedule
    from tests.schedule_factories import GROUP, KYIV

    await db.save_subscriber(42, "Оля", None, SubscriberStatus.APPROVED)
    snapshot = make_snapshot(0, soc=80)
    poller = SimpleNamespace(latest=snapshot, state=initial_state(snapshot))
    # on_status uses the real clock, so the outage is placed relative to it.
    start = datetime.now(UTC) + timedelta(hours=2)
    outage = Outage(start, start + timedelta(hours=4))
    day = start.astimezone(KYIV).date()
    schedule = Schedule(GROUP, {day: DaySchedule(GROUP, day, (outage,))})
    schedules = SimpleNamespace(current=lambda now: schedule)
    msg = message(42)
    await on_status(msg, db=db, settings=make_settings(ADMIN), poller=poller, schedules=schedules)
    assert "📅 Наступне відключення:" in msg.answer.await_args.args[0]
```

(Move the two imports to the top of the file with the others. `message`, `ADMIN`, `make_snapshot` and
`initial_state` already exist in that test module; reuse them.)

Append to `tests/test_main.py`:

```python
async def test_stop_background_cancels_extra_tasks(db):
    notifier = Notifier(SimpleNamespace(send_message=AsyncMock()), db, 1, ZoneInfo("Europe/Kyiv"))
    poller_task = asyncio.create_task(asyncio.sleep(3600))
    notifier_task = asyncio.create_task(notifier.run())
    schedule_task = asyncio.create_task(asyncio.sleep(3600))
    await stop_background(poller_task, notifier_task, notifier, 0.1, others=[schedule_task])
    assert schedule_task.cancelled()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py tests/test_handlers.py tests/test_main.py -q`
Expected: FAIL (`Settings` has no `schedule`; `on_status` has no `schedules`; `stop_background` has no
`others`).

- [ ] **Step 3: Extend `lxbot/config.py`**

Add the imports:

```python
from datetime import time, timedelta

from lxbot.schedule.parse import group_key
from lxbot.schedule.service import DEFAULT_URL, ScheduleConfig
```

Add the field `schedule: ScheduleConfig | None = None` as the last field of `Settings`. Then add:

```python
def _clock_time(env: Mapping[str, str], name: str, default: str) -> time:
    value = env.get(name, default)
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be HH:MM") from exc


def schedule_config_from_env(env: Mapping[str, str]) -> ScheduleConfig | None:
    queue = env.get("SCHEDULE_GROUP", "").strip()
    if not queue:
        return None
    config = ScheduleConfig(
        group=group_key(queue),
        url=env.get("SCHEDULE_URL") or DEFAULT_URL,
        poll_interval=timedelta(seconds=_number(env, "SCHEDULE_POLL_INTERVAL", "900", float)),
        match_window=timedelta(minutes=_number(env, "SCHEDULE_MATCH_WINDOW", "30", float)),
        today_at=_clock_time(env, "DIGEST_TODAY_AT", "09:00"),
        tomorrow_at=_clock_time(env, "DIGEST_TOMORROW_AT", "21:00"),
    )
    if not config.today_at < config.tomorrow_at:
        raise ConfigError("DIGEST_TODAY_AT must be earlier than DIGEST_TOMORROW_AT")
    return config
```

and pass `schedule=schedule_config_from_env(env)` in `settings_from_env`'s `Settings(...)`.

- [ ] **Step 4: Extend `lxbot/bot/handlers.py` `on_status`**

```python
async def on_status(message: Message, db: Database, settings: Settings, poller, schedules=None) -> None:
    if not await subscriptions.is_approved(db, message.chat.id):
        await message.answer(texts.NOT_SUBSCRIBED)
        return
    now = datetime.now(UTC)
    schedule = schedules.current(now) if schedules is not None else None
    await message.answer(texts.status_text(poller.latest, poller.state, now, settings.tz, schedule))
```

- [ ] **Step 5: Wire it in `lxbot/main.py`**

Add the imports:

```python
from collections.abc import Sequence

from lxbot.schedule.annotate import ScheduleAnnotator
from lxbot.schedule.service import ScheduleService
from lxbot.schedule.source import HttpScheduleSource
```

`stop_background` gains the extra tasks:

```python
async def stop_background(
    poller_task: asyncio.Task,
    notifier_task: asyncio.Task,
    notifier: Notifier,
    drain_seconds: float = SHUTDOWN_DRAIN_SECONDS,
    *,
    others: Sequence[asyncio.Task] = (),
) -> None:
    poller_task.cancel()
    for task in others:
        task.cancel()
    # An alert queued just before a restart still goes out, unless Telegram is unreachable.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(notifier.join(), drain_seconds)
    notifier_task.cancel()
    await asyncio.gather(poller_task, notifier_task, *others, return_exceptions=True)
```

In `run()`:
1. Build the schedule pieces after the notifier.
2. Give the poller the annotator as its sink.
3. Pass `schedules` to the dispatcher.
4. Load and start the schedule task.
5. Stop and close it in `finally`.

The body becomes:

```python
async def run(settings: Settings) -> None:
    db = await Database.open(settings.db_path)
    client = DongleClient(settings.dongle)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db, settings.admin_chat_id, settings.tz)
    source = HttpScheduleSource(settings.schedule.url) if settings.schedule else None
    schedules = ScheduleService(source, db, notifier, settings.schedule, settings.tz) if source else None
    sink = ScheduleAnnotator(notifier, schedules, settings.schedule.match_window) if schedules else notifier
    poller = Poller(client, db, sink, settings.thresholds, settings.poll_interval)
    dispatcher = Dispatcher(db=db, settings=settings, poller=poller, schedules=schedules)
    dispatcher.include_router(router)
    background: list[asyncio.Task] = []
    on_done = stop_when_ended(dispatcher, asyncio.current_task())
    try:
        await poller.load()
        if schedules is not None:
            await schedules.load()
        background = [
            asyncio.create_task(poller.run(), name="poller"),
            asyncio.create_task(notifier.run(), name="notifier"),
        ]
        if schedules is not None:
            background.append(asyncio.create_task(schedules.run(), name="schedule"))
        for task in background:
            task.add_done_callback(on_done)
        await wait_for_telegram(bot)
        await poll_telegram(dispatcher, bot)
    finally:
        for task in background:
            task.remove_done_callback(on_done)
        if background:
            await stop_background(background[0], background[1], notifier, others=background[2:])
        if source is not None:
            await source.close()
        await client.close()
        await db.close()
        await bot.session.close()
```

- [ ] **Step 6: Document it**

Append to `.env.example`:

```
# Planned outage schedule (DTEK, via github.com/Baskerville42/outage-data-ua); empty turns it off
SCHEDULE_GROUP=3.2
SCHEDULE_MATCH_WINDOW=30
DIGEST_TODAY_AT=09:00
DIGEST_TOMORROW_AT=21:00
```

Add a section to `README.md` after "Commands":

```markdown
## Outage schedule

With `SCHEDULE_GROUP` set (e.g. `3.2`), the bot follows the DTEK KREM planned outage schedule for that
queue. The source is [outage-data-ua](https://github.com/Baskerville42/outage-data-ua) (MIT), which
scrapes DTEK every ~5 minutes; it is polled every 15 minutes.

- 09:00: today's schedule; 21:00: tomorrow's (or "not published yet", then a message when it appears).
- "🔄 Графік змінився" when a day's schedule changes after its digest went out.
- Grid lost / restored messages say whether the change matches the schedule (±30 min) and what is
  expected next; `/status` shows the next planned change.
- The admin is told when the source stops updating for an hour.
```

- [ ] **Step 7: Run all tests**

Run: `.venv/bin/pytest -q -W error`
Expected: all pass. Also run `env -i PATH="$PATH" .venv/bin/python -m lxbot.main` and expect the same
ConfigError for missing variables as before.

- [ ] **Step 8: Commit**

```bash
git add lxbot/config.py lxbot/bot/handlers.py lxbot/main.py .env.example README.md \
  tests/test_config.py tests/test_handlers.py tests/test_main.py
git commit -m "Wire the outage schedule into settings, /status and startup

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Deploy to rock2 (operations, needs the user's OK)

Not code.

- [ ] Push `feat/outage-schedule` and open a PR into `main` (ask first). Before pushing, run a leak check
  with the real values from rock2's `.env`, outside the repo; it must print nothing.
- [ ] After the merge, on rock2:
  1. `cd /root/luxpower-tg-bot && git pull`;
  2. append `SCHEDULE_GROUP=3.2` to `.env` (not a secret);
  3. `docker compose up -d --build` and check the logs for `schedule refresh failed` (there should be
     none).
- [ ] Check in Telegram: `/status` shows the "📅" line. The next 21:00 or 09:00 digest arrives once.
- [ ] At the next outage, the "Світло зникло" message carries the "⏱ За графіком…" or "⚠️ Не за графіком"
  line.
