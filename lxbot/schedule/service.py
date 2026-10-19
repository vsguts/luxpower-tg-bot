import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from lxbot.monitor.detector import Event, EventType
from lxbot.monitor.poller import EventSink
from lxbot.schedule.model import DaySchedule, Schedule, ScheduleUpdate
from lxbot.schedule.parse import ScheduleParseError, parse_schedule
from lxbot.schedule.source import ScheduleSource, ScheduleSourceError
from lxbot.storage.db import Database

log = logging.getLogger(__name__)

DEFAULT_URL = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json"
SCHEDULE_KEY = "schedule"
# The row keeps the name it had while the schedule went out as fixed-time digests; load() migrates it.
POSTED_KEY = "schedule-digests"
HEALTH_KEY = "schedule-health"
TICK_SECONDS = 60.0
SCHEDULE_STALE_AFTER = timedelta(hours=6)
SOURCE_MAX_AGE = timedelta(hours=1)
SOURCE_DOWN_AFTER = timedelta(hours=1)
# A schedule that keeps changing is still posted this long after it started to differ from what was posted.
SETTLE_MAX = timedelta(minutes=45)
# Schedule messages in these hours go out without a sound; grid and battery alerts always ring.
QUIET_FROM = time(23)
QUIET_UNTIL = time(7)
# Every 304 moves fetched_at; writing it each time wears the microSD card. A restart may then see it up
# to this much older, which the 6 h staleness limit absorbs.
FETCHED_AT_SAVE_STEP = timedelta(hours=1)


@dataclass(frozen=True)
class ScheduleConfig:
    group: str
    url: str = DEFAULT_URL
    # The source scrapes DTEK every 5 minutes; polling faster only rereads the same document.
    poll_interval: timedelta = timedelta(minutes=5)
    match_window: timedelta = timedelta(minutes=30)
    # DTEK sometimes publishes an empty day first and the real one minutes later: a version must hold
    # this long before it is posted.
    settle: timedelta = timedelta(minutes=20)


def fingerprint(schedule: DaySchedule) -> str:
    return json.dumps([outage.to_list() for outage in schedule.outages])


# Per day, the fingerprint of what the family was last shown. None: shown by a fixed-time digest written
# before fingerprints existed, adopted silently.
def _posted_from_row(row: dict) -> dict[date, str | None]:
    stored = row["posted"] if "posted" in row else row.get("announced") or {}
    posted = {date.fromisoformat(day): told for day, told in stored.items()}
    # Older digest rows had only the dates of the last morning and evening digests.
    for key in ("today", "tomorrow"):
        if row.get(key) and not (key == "tomorrow" and row[key] == row.get("pending")):
            posted.setdefault(date.fromisoformat(row[key]), None)
    return posted


def _posted_row(posted: dict[date, str | None]) -> dict:
    return {"posted": {day.isoformat(): told for day, told in sorted(posted.items())}}


# lastUpdated can move with every scrape: on its own it is saved with the hourly fetch time, not each poll.
def _content(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in ("dtek_updated", "source_updated")}


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
        self._parse_failed = False
        self._failing_since: datetime | None = None
        self._down_reported = False
        self._saved_schedule: dict | None = None
        self._saved_fetched_at: datetime | None = None
        self._posted: dict[date, str | None] = {}
        self._saved_posted = _posted_row(self._posted)
        # Per day: (fingerprint of the current version, since when it is the current one).
        self._versions: dict[date, tuple[str, datetime]] = {}
        # Per day: since when it differs from what was posted, or since it appeared unposted.
        self._diverged: dict[date, datetime] = {}

    @property
    def config(self) -> ScheduleConfig:
        return self._config

    def current(self, now: datetime) -> Schedule | None:
        schedule = self._schedule
        if schedule is None or self._fetched_at is None or now - self._fetched_at > SCHEDULE_STALE_AFTER:
            return None
        # A frozen source keeps answering 304 or the same document, so fetched_at alone never ages out.
        if self._failing_since is not None and now - self._failing_since >= SCHEDULE_STALE_AFTER:
            return None
        # Without today every grid change would read "not on schedule" and /status "nothing planned".
        if schedule.day(self._local(now).date()) is None:
            return None
        return schedule

    # A broken row starts its part empty: the schedule is refetched within a tick, while a crash here
    # would stop the whole bot on every start.
    async def load(self) -> None:
        for key, restore in (
            (SCHEDULE_KEY, self._restore_schedule),
            (POSTED_KEY, self._restore_posted),
            (HEALTH_KEY, self._restore_health),
        ):
            stored = await self._db.load_state(key)
            if not stored:
                continue
            try:
                restore(stored)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                log.warning("ignoring the stored schedule state %r: %r", key, exc)

    def _restore_schedule(self, stored: dict) -> None:
        schedule = Schedule(
            group=stored["group"],
            days={d.day: d for d in map(DaySchedule.from_dict, stored["days"])},
            dtek_updated=_optional_time(stored["dtek_updated"]),
            source_ok=stored["source_ok"],
            source_updated=_optional_time(stored["source_updated"]),
        )
        fetched_at = datetime.fromisoformat(stored["fetched_at"])
        self._schedule, self._fetched_at = schedule, fetched_at
        self._saved_schedule, self._saved_fetched_at = _content(self._schedule_row()), fetched_at
        self._versions = {day: (fingerprint(d), fetched_at) for day, d in schedule.days.items()}

    def _restore_posted(self, stored: dict) -> None:
        self._posted = _posted_from_row(stored)
        self._saved_posted = _posted_row(self._posted)

    def _restore_health(self, stored: dict) -> None:
        failing_since = _optional_time(stored["failing_since"])
        self._failing_since, self._down_reported = failing_since, stored["down_reported"]

    async def run(self) -> None:
        while True:
            try:
                await self.tick(self._clock())
            except Exception:
                log.exception("schedule tick failed")
            await asyncio.sleep(self._tick_seconds)

    async def tick(self, now: datetime) -> None:
        if self._last_attempt is None or now - self._last_attempt >= self._config.poll_interval:
            await self._refresh(now)
        events = await self._health_events(now)
        if self._schedule is not None:
            events += self._update_events(now)
            await self._save_posted()
        if self._quiet(now):
            events = [replace(e, silent=True) for e in events]
        if events:
            await self._sink.publish(events)

    def _quiet(self, now: datetime) -> bool:
        clock = self._local(now).time()
        return clock >= QUIET_FROM or clock < QUIET_UNTIL

    def _local(self, now: datetime) -> datetime:
        return now.astimezone(self._tz)

    async def _refresh(self, now: datetime) -> None:
        self._last_attempt = now
        try:
            document = await self._source.fetch()
            fresh = None if document is None else parse_schedule(document, self._config.group, self._tz)
        except (ScheduleSourceError, ScheduleParseError) as exc:
            log.warning("schedule refresh failed: %s", exc)
            self._fetch_ok = False
            self._parse_failed = isinstance(exc, ScheduleParseError) or self._parse_failed
            return
        # The source remembers the ETag of a document we failed to parse, so its next answer is a 304
        # that must not pass for a successful refresh.
        if fresh is None and self._parse_failed:
            self._fetch_ok = False
            return
        self._parse_failed = False
        self._fetch_ok = True
        self._fetched_at = now
        if fresh is not None and self._older_than_stored(fresh):
            # CDN edges or a flipping scraper can serve the previous document again: treat it as a 304.
            log.info("ignoring an older copy of the schedule (updated %s)", fresh.source_updated)
            fresh = None
        if fresh is not None:
            self._schedule = fresh
        await self._save_schedule()

    def _older_than_stored(self, fresh: Schedule) -> bool:
        stored = self._schedule.source_updated if self._schedule else None
        return stored is not None and fresh.source_updated is not None and fresh.source_updated < stored

    # One message whenever a day appears or a posted day changes, with every known day from today on. Each
    # waits until it settles. A new day that has not settled stays out of the message; a posted day that
    # has not settled holds the message back, so it never shows a version that is about to change back.
    def _update_events(self, now: datetime) -> list[Event]:
        today = self._local(now).date()
        self._forget_before(today)
        schedule = self._schedule
        shown, published, changed, due, waiting = [], [], [], False, False
        for day in sorted(schedule.days.keys() | self._posted.keys() | self._versions.keys()):
            if day < today:
                continue
            current = schedule.day(day)
            # Missing from the document, or stood in for by an empty one without ever being published: not
            # news. Should it come back, it settles afresh.
            if current is None or (day not in self._posted and day not in schedule.days):
                self._versions.pop(day, None)
                self._diverged.pop(day, None)
                continue
            version = fingerprint(current)
            self._note_version(day, version, now)
            told = self._posted.get(day)
            if day in self._posted and told in (None, version):
                self._posted[day] = version
                self._diverged.pop(day, None)
                shown.append(current)
                continue
            if not self._settled(day, now, self._diverged.setdefault(day, now)):
                waiting = waiting or day in self._posted
                continue
            due = True
            shown.append(current)
            (changed if day in self._posted else published).append(day)
        if not due or waiting:
            return []
        for current in shown:
            self._posted[current.day] = fingerprint(current)
            self._diverged.pop(current.day, None)
        update = ScheduleUpdate(tuple(shown), tuple(published), tuple(changed))
        return [Event(EventType.SCHEDULE_UPDATE, now, update=update, source_updated=self._outdated_since(now))]

    def _forget_before(self, today: date) -> None:
        for days in (self._posted, self._versions, self._diverged):
            for day in [d for d in days if d < today]:
                del days[day]

    def _note_version(self, day: date, version: str, now: datetime) -> None:
        known = self._versions.get(day)
        if known is None or known[0] != version:
            self._versions[day] = (version, now)

    # A version is ready once it has held for the settle time, or once the churn has lasted SETTLE_MAX. An
    # empty document cancels every posted day at once, so a blank page at the source must hold for longer.
    def _settled(self, day: date, now: datetime, churn_since: datetime) -> bool:
        hold = self._config.settle if self._schedule.days else SETTLE_MAX
        return now - self._versions[day][1] >= hold or now - churn_since >= SETTLE_MAX

    # Updates still go out from data the source stopped refreshing, saying how old it is.
    def _outdated_since(self, now: datetime) -> datetime | None:
        updated = self._schedule.source_updated or self._fetched_at
        return updated if now - updated > SOURCE_MAX_AGE else None

    def _healthy(self, now: datetime) -> bool:
        if not self._fetch_ok or self._schedule is None or not self._schedule.source_ok:
            return False
        updated = self._schedule.source_updated
        return updated is None or now - updated <= SOURCE_MAX_AGE

    async def _health_events(self, now: datetime) -> list[Event]:
        before = (self._failing_since, self._down_reported)
        events = self._evaluate_health(now)
        if (self._failing_since, self._down_reported) != before:
            await self._db.save_state(
                HEALTH_KEY,
                {
                    "failing_since": self._failing_since.isoformat() if self._failing_since else None,
                    "down_reported": self._down_reported,
                },
            )
        return events

    def _evaluate_health(self, now: datetime) -> list[Event]:
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
        return [Event(EventType.SCHEDULE_SOURCE_DOWN, self._stopped_since())]

    # A stale source is noticed an hour after its last update; the admin wants the latter.
    def _stopped_since(self) -> datetime:
        updated = self._schedule.source_updated if self._schedule else None
        return min(self._failing_since, updated) if updated else self._failing_since

    def _schedule_row(self) -> dict:
        schedule = self._schedule
        return {
            "group": schedule.group,
            "days": [d.to_dict() for d in schedule.days.values()],
            "dtek_updated": schedule.dtek_updated.isoformat() if schedule.dtek_updated else None,
            "source_ok": schedule.source_ok,
            "source_updated": schedule.source_updated.isoformat() if schedule.source_updated else None,
        }

    async def _save_schedule(self) -> None:
        if self._schedule is None:
            return
        row = self._schedule_row()
        lagging = self._saved_fetched_at is None or self._fetched_at - self._saved_fetched_at >= FETCHED_AT_SAVE_STEP
        if _content(row) == self._saved_schedule and not lagging:
            return
        await self._db.save_state(SCHEDULE_KEY, row | {"fetched_at": self._fetched_at.isoformat()})
        self._saved_schedule, self._saved_fetched_at = _content(row), self._fetched_at

    async def _save_posted(self) -> None:
        row = _posted_row(self._posted)
        if row != self._saved_posted:
            await self._db.save_state(POSTED_KEY, row)
            self._saved_posted = row
