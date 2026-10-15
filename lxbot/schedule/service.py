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
HEALTH_KEY = "schedule-health"
TICK_SECONDS = 60.0
SCHEDULE_STALE_AFTER = timedelta(hours=6)
REFRESH_BEFORE_DIGEST = timedelta(minutes=15)
DIGEST_RETRY = timedelta(minutes=5)
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
        self._parse_failed = False
        self._failing_since: datetime | None = None
        self._down_reported = False

    @property
    def config(self) -> ScheduleConfig:
        return self._config

    def current(self, now: datetime) -> Schedule | None:
        if self._schedule is None or self._fetched_at is None or now - self._fetched_at > SCHEDULE_STALE_AFTER:
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
        health = await self._db.load_state(HEALTH_KEY)
        if health:
            self._failing_since = _optional_time(health["failing_since"])
            self._down_reported = health["down_reported"]

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
        events += await self._digest_events(now)
        if events:
            await self._sink.publish(events)

    def _local(self, now: datetime) -> datetime:
        return now.astimezone(self._tz)

    def _refresh_due(self, now: datetime) -> bool:
        if self._last_attempt is None or now - self._last_attempt >= self._config.poll_interval:
            return True
        # A digest goes out from the freshest data, not from a fetch up to 15 min old. The retry
        # pause keeps a dead source from being hit (and logged) every minute while a digest is due.
        outdated = self._fetched_at is None or now - self._fetched_at > REFRESH_BEFORE_DIGEST
        return outdated and now - self._last_attempt >= DIGEST_RETRY and self._digest_due(now)

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
            if self._announced(day, today):
                events.append(Event(EventType.SCHEDULE_CHANGED, now, schedule=schedule))
        return events

    def _announced(self, day: date, today: date) -> bool:
        digests = self._digests
        if day == today:
            # After midnight the previous evening's digest is the announcement for the new today.
            return digests.today == today or (digests.tomorrow == today and digests.pending != today)
        tomorrow = today + timedelta(days=1)
        return day == tomorrow and digests.tomorrow == tomorrow and digests.pending != tomorrow

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
