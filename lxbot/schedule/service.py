import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
HEALTH_KEY = "schedule-health"
TICK_SECONDS = 60.0
SCHEDULE_STALE_AFTER = timedelta(hours=6)
REFRESH_BEFORE_DIGEST = timedelta(minutes=15)
DIGEST_RETRY = timedelta(minutes=5)
SOURCE_MAX_AGE = timedelta(hours=1)
SOURCE_DOWN_AFTER = timedelta(hours=1)
# DTEK publishes tomorrow's schedule anywhere between ~19:30 and past midnight: wait for it from the
# evening on, polling as often as the source scrapes (every 5 min) until shortly after midnight.
EVENING_FROM = time(18)
EVENING_UNTIL = time(1)
EVENING_POLL = timedelta(minutes=5)
# A schedule that keeps changing is still announced this long after it first appeared.
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
    poll_interval: timedelta = timedelta(minutes=15)
    match_window: timedelta = timedelta(minutes=30)
    today_at: time = time(9)
    pending_at: time = time(22)
    # DTEK sometimes publishes an empty day first and the real one minutes later: a version must hold
    # this long before it is announced.
    settle: timedelta = timedelta(minutes=20)


@dataclass
class _Digests:
    today: date | None = None
    pending: date | None = None
    # What the family was last told about each day, as a fingerprint of its outages. None: told, but by a
    # version written before fingerprints existed; it is adopted silently.
    announced: dict[date, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "today": self.today.isoformat() if self.today else None,
            "pending": self.pending.isoformat() if self.pending else None,
            "announced": {day.isoformat(): told for day, told in sorted(self.announced.items())},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "_Digests":
        def day(key: str) -> date | None:
            return date.fromisoformat(data[key]) if data.get(key) else None

        announced = {date.fromisoformat(d): told for d, told in (data.get("announced") or {}).items()}
        pending = day("pending")
        # Rows from the fixed 21:00 digest marked the evening's announcement as "tomorrow".
        if day("tomorrow") and day("tomorrow") != pending:
            announced.setdefault(day("tomorrow"), None)
        if day("today"):
            announced.setdefault(day("today"), None)
        return cls(today=day("today"), pending=pending, announced=announced)


def _fingerprint(schedule: DaySchedule) -> str:
    return json.dumps([outage.to_list() for outage in schedule.outages])


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
        self._parse_failed = False
        self._failing_since: datetime | None = None
        self._down_reported = False
        self._saved_schedule: dict | None = None
        self._saved_fetched_at: datetime | None = None
        self._saved_digests = self._digests.to_dict()
        # Per day: (fingerprint of the current version, when that version appeared, when the day appeared).
        self._versions: dict[date, tuple[str, datetime, datetime]] = {}
        # Per announced day: since when the schedule differs from what the family was told.
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
            (DIGESTS_KEY, self._restore_digests),
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
        self._saved_schedule, self._saved_fetched_at = self._schedule_row(), fetched_at
        self._versions = {day: (_fingerprint(d), fetched_at, fetched_at) for day, d in schedule.days.items()}

    def _restore_digests(self, stored: dict) -> None:
        self._digests = _Digests.from_dict(stored)
        self._saved_digests = self._digests.to_dict()

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
        events: list[Event] = []
        if self._refresh_due(now):
            events += await self._refresh(now)
        events += await self._health_events(now)
        if self._schedule is not None:
            # The digests first: a change settling in the same tick is then part of them, not a second message.
            events += self._evening_events(now)
            events += self._morning_events(now)
            events += self._change_events(now)
            await self._save_digests()
        events = self._with_source_age(events)
        if self._quiet(now):
            events = [replace(e, silent=True) for e in events]
        if events:
            await self._sink.publish(events)

    def _quiet(self, now: datetime) -> bool:
        clock = self._local(now).time()
        return clock >= QUIET_FROM or clock < QUIET_UNTIL

    # Digests and change notices still go out while the source is unhealthy, but say how old the data may be.
    def _with_source_age(self, events: list[Event]) -> list[Event]:
        if self._failing_since is None or self._schedule is None:
            return events
        updated = self._schedule.source_updated or self._fetched_at
        return [replace(e, source_updated=updated) if e.schedule is not None else e for e in events]

    def _local(self, now: datetime) -> datetime:
        return now.astimezone(self._tz)

    def _refresh_due(self, now: datetime) -> bool:
        clock = self._local(now).time()
        evening = clock >= EVENING_FROM or clock < EVENING_UNTIL
        interval = min(EVENING_POLL, self._config.poll_interval) if evening else self._config.poll_interval
        if self._last_attempt is None or now - self._last_attempt >= interval:
            return True
        # The morning digest goes out from the freshest data, not from a fetch up to 15 min old. The retry
        # pause keeps a dead source from being hit (and logged) every minute while the digest is due.
        outdated = self._fetched_at is None or now - self._fetched_at > REFRESH_BEFORE_DIGEST
        return outdated and now - self._last_attempt >= DIGEST_RETRY and self._morning_due(now)

    async def _refresh(self, now: datetime) -> list[Event]:
        self._last_attempt = now
        try:
            document = await self._source.fetch()
            fresh = None if document is None else parse_schedule(document, self._config.group, self._tz)
        except (ScheduleSourceError, ScheduleParseError) as exc:
            log.warning("schedule refresh failed: %s", exc)
            self._fetch_ok = False
            self._parse_failed = isinstance(exc, ScheduleParseError) or self._parse_failed
            return []
        # The source remembers the ETag of a document we failed to parse, so its next answer is a 304
        # that must not pass for a successful refresh.
        if fresh is None and self._parse_failed:
            self._fetch_ok = False
            return []
        self._parse_failed = False
        self._fetch_ok = True
        self._fetched_at = now
        if fresh is not None and self._older_than_stored(fresh):
            # CDN edges or a flipping scraper can serve the previous document again: treat it as a 304.
            log.info("ignoring an older copy of the schedule (updated %s)", fresh.source_updated)
            fresh = None
        if fresh is not None:
            self._track_versions(fresh, now)
            self._schedule = fresh
        await self._save()
        return []

    def _track_versions(self, fresh: Schedule, now: datetime) -> None:
        for day, schedule in fresh.days.items():
            fingerprint = _fingerprint(schedule)
            known = self._versions.get(day)
            if known is None:
                self._versions[day] = (fingerprint, now, now)
            elif known[0] != fingerprint:
                self._versions[day] = (fingerprint, now, known[2])
        today = self._local(now).date()
        self._versions = {day: v for day, v in self._versions.items() if day >= today - timedelta(days=1)}

    def _appeared(self, day: date, now: datetime) -> datetime:
        version = self._versions.get(day)
        return version[2] if version else now

    # A version is ready once it has held for the settle time, or once the churn has lasted SETTLE_MAX.
    def _settled(self, day: date, now: datetime, churn_since: datetime) -> bool:
        version = self._versions.get(day)
        if version is None:
            return False
        return now - version[1] >= self._config.settle or now - churn_since >= SETTLE_MAX

    def _older_than_stored(self, fresh: Schedule) -> bool:
        stored = self._schedule.source_updated if self._schedule else None
        return stored is not None and fresh.source_updated is not None and fresh.source_updated < stored

    def _change_events(self, now: datetime) -> list[Event]:
        today = self._local(now).date()
        announced = self._digests.announced
        for day in [d for d in announced if d < today - timedelta(days=1)]:
            del announced[day]
        for day in [d for d in self._diverged if d not in announced or self._schedule.day(d) is None]:
            del self._diverged[day]
        events = []
        for day, told in sorted(announced.items()):
            schedule = self._schedule.day(day)
            if day < today or schedule is None:
                continue
            fingerprint = _fingerprint(schedule)
            if told is None or fingerprint == told:
                announced[day] = fingerprint
                self._diverged.pop(day, None)
                continue
            if self._settled(day, now, self._diverged.setdefault(day, now)):
                announced[day] = fingerprint
                self._diverged.pop(day, None)
                events.append(Event(EventType.SCHEDULE_CHANGED, now, schedule=schedule))
        return events

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

    def _morning_due(self, now: datetime) -> bool:
        local = self._local(now)
        return self._config.today_at <= local.time() < EVENING_FROM and self._digests.today != local.date()

    def _morning_events(self, now: datetime) -> list[Event]:
        if not self._morning_due(now):
            return []
        today = self._local(now).date()
        schedule = self._schedule.day(today)
        if schedule is None:
            return []
        self._digests.today = today
        self._digests.announced[today] = _fingerprint(schedule)
        return [Event(EventType.SCHEDULE_TODAY, now, schedule=schedule)]

    # Tomorrow's schedule goes out once a version has settled, whenever DTEK publishes it. One published
    # only after midnight is today's by then and goes out as such, before the morning digest.
    def _evening_target(self, now: datetime) -> date | None:
        local = self._local(now)
        if local.time() >= EVENING_FROM:
            return local.date() + timedelta(days=1)
        today, clock, digests = local.date(), local.time(), self._digests
        if clock >= self._config.today_at or today in digests.announced or digests.today == today:
            return None
        # Announced as missing at 22:00: it goes out whenever it settles before the morning digest. Never
        # announced at all (the source was down at 22:00): only a night outage cannot wait for 09:00.
        if digests.pending == today or clock < QUIET_UNTIL:
            return today
        return None

    def _evening_events(self, now: datetime) -> list[Event]:
        target = self._evening_target(now)
        digests = self._digests
        if target is None or target in digests.announced:
            return []
        today = self._local(now).date()
        schedule = self._schedule.day(target)
        if schedule is None:
            # An unhealthy source may just have missed the publication: do not tell the family it is not out.
            past_deadline = self._local(now).time() >= self._config.pending_at
            if target != today and past_deadline and digests.pending != target and self._failing_since is None:
                digests.pending = target
                return [Event(EventType.SCHEDULE_TOMORROW_PENDING, now)]
            return []
        if not self._settled(target, now, self._appeared(target, now)):
            return []
        digests.announced[target] = _fingerprint(schedule)
        if target == today:
            digests.today, digests.pending = today, None
            return [Event(EventType.SCHEDULE_TODAY, now, schedule=schedule)]
        kind = EventType.SCHEDULE_TOMORROW_PUBLISHED if digests.pending == target else EventType.SCHEDULE_TOMORROW
        digests.pending = None
        return [Event(kind, now, schedule=schedule)]

    async def _save(self) -> None:
        await self._save_schedule()
        await self._save_digests()

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
        if row == self._saved_schedule and not lagging:
            return
        await self._db.save_state(SCHEDULE_KEY, row | {"fetched_at": self._fetched_at.isoformat()})
        self._saved_schedule, self._saved_fetched_at = row, self._fetched_at

    async def _save_digests(self) -> None:
        digests = self._digests.to_dict()
        if digests != self._saved_digests:
            await self._db.save_state(DIGESTS_KEY, digests)
            self._saved_digests = digests
