"""Turns the snapshot stream into grid, generator and battery events. Pure: no I/O."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from lxbot.device.snapshot import Snapshot
from lxbot.monitor.forecast import BatteryConfig, Forecast, forecast_for
from lxbot.schedule.compare import ScheduleMatch
from lxbot.schedule.model import DaySchedule


class EventType(StrEnum):
    GRID_LOST = "grid_lost"
    GRID_RESTORED = "grid_restored"
    BATTERY_LOW = "battery_low"
    BATTERY_CRITICAL = "battery_critical"
    GEN_CHARGED = "gen_charged"
    GEN_STARTED = "gen_started"
    GEN_STOPPED = "gen_stopped"
    # The events journal keeps the names from when the only device was the LuxPower dongle.
    DEVICE_DOWN = "dongle_down"
    DEVICE_UP = "dongle_up"
    SCHEDULE_TODAY = "schedule_today"
    SCHEDULE_TOMORROW = "schedule_tomorrow"
    SCHEDULE_TOMORROW_PENDING = "schedule_tomorrow_pending"
    SCHEDULE_TOMORROW_PUBLISHED = "schedule_tomorrow_published"
    SCHEDULE_CHANGED = "schedule_changed"
    SCHEDULE_SOURCE_DOWN = "schedule_source_down"
    SCHEDULE_SOURCE_UP = "schedule_source_up"


class Audience(StrEnum):
    SUBSCRIBERS = "subscribers"
    ADMIN = "admin"
    JOURNAL = "journal"


_AUDIENCES = {
    EventType.GEN_STARTED: Audience.JOURNAL,
    EventType.GEN_STOPPED: Audience.JOURNAL,
    EventType.DEVICE_DOWN: Audience.ADMIN,
    EventType.DEVICE_UP: Audience.ADMIN,
    EventType.SCHEDULE_SOURCE_DOWN: Audience.ADMIN,
    EventType.SCHEDULE_SOURCE_UP: Audience.ADMIN,
}


@dataclass(frozen=True)
class Event:
    type: EventType
    at: datetime
    soc: int | None = None
    previous_duration: timedelta | None = None
    gen_was_running: bool = False
    schedule: DaySchedule | None = None
    match: ScheduleMatch | None = None
    source_updated: datetime | None = None
    forecast: Forecast | None = None

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
        if self.source_updated is not None:
            payload["source_updated"] = self.source_updated.isoformat()
        if self.forecast is not None:
            payload["forecast"] = self.forecast.to_payload()
        return payload


@dataclass(frozen=True)
class Thresholds:
    soc_low: int = 30
    soc_critical: int = 15
    soc_gen_full: int = 90
    rearm_margin: int = 5
    confirm_polls: int = 3
    battery_confirm_polls: int = 2
    max_streak_age: timedelta = timedelta(minutes=2)
    battery: BatteryConfig | None = None


@dataclass(frozen=True)
class Debounced:
    value: bool
    since: datetime
    pending_since: datetime | None = None
    pending_count: int = 0

    def observe(self, observed: bool, at: datetime, confirm_polls: int, max_streak_age: timedelta) -> "Debounced":
        if observed == self.value:
            return Debounced(self.value, self.since)
        # A streak broken by a restart or a dongle outage says nothing about the time in between.
        if self.pending_since is None or at - self.pending_since > max_streak_age:
            pending_since, pending_count = at, 1
        else:
            pending_since, pending_count = self.pending_since, self.pending_count + 1
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
        pending_since = data.get("pending_since")
        return cls(
            value=data["value"],
            since=datetime.fromisoformat(data["since"]),
            pending_since=datetime.fromisoformat(pending_since) if pending_since else None,
            pending_count=data.get("pending_count", 0),
        )


@dataclass(frozen=True)
class State:
    grid: Debounced
    gen: Debounced
    low_sent: bool = False
    critical_sent: bool = False
    gen_charged_sent: bool = False
    low_streak: int = 0
    critical_streak: int = 0
    # Start of the current stretch on battery alone (no grid, no generator), for the discharge trend.
    drain_soc: int | None = None
    drain_since: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "grid": self.grid.to_dict(),
            "gen": self.gen.to_dict(),
            "low_sent": self.low_sent,
            "critical_sent": self.critical_sent,
            "gen_charged_sent": self.gen_charged_sent,
            "low_streak": self.low_streak,
            "critical_streak": self.critical_streak,
            "drain_soc": self.drain_soc,
            "drain_since": self.drain_since.isoformat() if self.drain_since else None,
        }

    # Rows written by an older version lack the newer fields; they start from the defaults.
    @classmethod
    def from_dict(cls, data: dict) -> "State":
        return cls(
            grid=Debounced.from_dict(data["grid"]),
            gen=Debounced.from_dict(data["gen"]),
            low_sent=data.get("low_sent", False),
            critical_sent=data.get("critical_sent", False),
            gen_charged_sent=data.get("gen_charged_sent", False),
            low_streak=data.get("low_streak", 0),
            critical_streak=data.get("critical_streak", 0),
            drain_soc=data.get("drain_soc"),
            drain_since=datetime.fromisoformat(data["drain_since"]) if data.get("drain_since") else None,
        )


def initial_state(snapshot: Snapshot) -> State:
    return State(
        grid=Debounced(snapshot.grid_present, snapshot.taken_at),
        gen=Debounced(snapshot.gen_running, snapshot.taken_at),
    )


# One garbled SOC reading (60 -> 0 -> 60) must not raise an alarm, so a battery alert needs a streak.
# The streak is capped at the confirmation count: the stored state then stays unchanged on every poll
# of a long low-battery night, and the microSD card is spared a write each time.
def _streak(current: int, below: bool, confirm: int) -> int:
    return min(current + 1, confirm) if below else 0


def detect(state: State | None, snapshot: Snapshot, thresholds: Thresholds) -> tuple[State, list[Event]]:
    if state is None:
        state = initial_state(snapshot)
    soc = snapshot.soc
    events: list[Event] = []
    grid_observed = snapshot.grid_present_given(state.grid.value)
    at = snapshot.taken_at
    grid = state.grid.observe(grid_observed, at, thresholds.confirm_polls, thresholds.max_streak_age)
    gen = state.gen.observe(snapshot.gen_running, at, thresholds.confirm_polls, thresholds.max_streak_age)
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

    on_battery = not grid.value and not gen.value
    if not on_battery:
        new = replace(new, drain_soc=None, drain_since=None)
    # The trend runs from the stretch's highest charge, measured now: solar can lift the charge
    # mid-outage, and a state restored mid-outage has no starting charge to trust. A rise counts only
    # while the battery is actually charging, so one garbled high reading does not reset the trend.
    elif new.drain_since is None or (soc > new.drain_soc and snapshot.battery_w > 0):
        new = replace(new, drain_soc=soc, drain_since=at)
    forecast = forecast_for(
        snapshot,
        on_battery=on_battery,
        drain_soc=new.drain_soc,
        drain_since=new.drain_since,
        battery=thresholds.battery,
    )
    events = [replace(e, forecast=forecast) if e.type is EventType.GRID_LOST else e for e in events]

    battery_confirm = thresholds.battery_confirm_polls
    new = replace(
        new,
        low_streak=_streak(new.low_streak, on_battery and soc <= thresholds.soc_low, battery_confirm),
        critical_streak=_streak(new.critical_streak, on_battery and soc <= thresholds.soc_critical, battery_confirm),
    )
    if new.critical_streak >= battery_confirm and not new.critical_sent:
        events.append(Event(EventType.BATTERY_CRITICAL, at, soc, forecast=forecast))
        new = replace(new, critical_sent=True, low_sent=True)
    elif new.low_streak >= battery_confirm and not new.low_sent:
        events.append(Event(EventType.BATTERY_LOW, at, soc, forecast=forecast))
        new = replace(new, low_sent=True)

    if gen.value and not grid.value and soc >= thresholds.soc_gen_full and not new.gen_charged_sent:
        events.append(Event(EventType.GEN_CHARGED, at, soc))
        new = replace(new, gen_charged_sent=True)

    return new, events
