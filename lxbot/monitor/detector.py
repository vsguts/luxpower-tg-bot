"""Turns the snapshot stream into grid, generator and battery events. Pure: no I/O."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from lxbot.luxpower.registers import Snapshot


class EventType(StrEnum):
    GRID_LOST = "grid_lost"
    GRID_RESTORED = "grid_restored"
    BATTERY_LOW = "battery_low"
    BATTERY_CRITICAL = "battery_critical"
    GEN_CHARGED = "gen_charged"
    GEN_STARTED = "gen_started"
    GEN_STOPPED = "gen_stopped"
    DONGLE_DOWN = "dongle_down"
    DONGLE_UP = "dongle_up"


class Audience(StrEnum):
    SUBSCRIBERS = "subscribers"
    ADMIN = "admin"
    JOURNAL = "journal"


_AUDIENCES = {
    EventType.GEN_STARTED: Audience.JOURNAL,
    EventType.GEN_STOPPED: Audience.JOURNAL,
    EventType.DONGLE_DOWN: Audience.ADMIN,
    EventType.DONGLE_UP: Audience.ADMIN,
}


@dataclass(frozen=True)
class Event:
    type: EventType
    at: datetime
    soc: int | None = None
    previous_duration: timedelta | None = None
    gen_was_running: bool = False

    @property
    def audience(self) -> Audience:
        return _AUDIENCES.get(self.type, Audience.SUBSCRIBERS)

    def to_payload(self) -> dict:
        duration = self.previous_duration
        return {
            "soc": self.soc,
            "previous_duration_s": int(duration.total_seconds()) if duration is not None else None,
            "gen_was_running": self.gen_was_running,
        }


@dataclass(frozen=True)
class Thresholds:
    soc_low: int = 30
    soc_critical: int = 15
    soc_gen_full: int = 90
    rearm_margin: int = 5
    confirm_polls: int = 3


@dataclass(frozen=True)
class Debounced:
    value: bool
    since: datetime
    pending_since: datetime | None = None
    pending_count: int = 0

    def observe(self, observed: bool, at: datetime, confirm_polls: int) -> "Debounced":
        if observed == self.value:
            return Debounced(self.value, self.since)
        pending_since = self.pending_since or at
        pending_count = self.pending_count + 1
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
        pending_since = data["pending_since"]
        return cls(
            value=data["value"],
            since=datetime.fromisoformat(data["since"]),
            pending_since=datetime.fromisoformat(pending_since) if pending_since else None,
            pending_count=data["pending_count"],
        )


@dataclass(frozen=True)
class State:
    grid: Debounced
    gen: Debounced
    low_sent: bool = False
    critical_sent: bool = False
    gen_charged_sent: bool = False

    def to_dict(self) -> dict:
        return {
            "grid": self.grid.to_dict(),
            "gen": self.gen.to_dict(),
            "low_sent": self.low_sent,
            "critical_sent": self.critical_sent,
            "gen_charged_sent": self.gen_charged_sent,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        return cls(
            grid=Debounced.from_dict(data["grid"]),
            gen=Debounced.from_dict(data["gen"]),
            low_sent=data["low_sent"],
            critical_sent=data["critical_sent"],
            gen_charged_sent=data["gen_charged_sent"],
        )


def initial_state(snapshot: Snapshot) -> State:
    return State(
        grid=Debounced(snapshot.grid_present, snapshot.taken_at),
        gen=Debounced(snapshot.gen_running, snapshot.taken_at),
    )


def detect(state: State | None, snapshot: Snapshot, thresholds: Thresholds) -> tuple[State, list[Event]]:
    if state is None:
        state = initial_state(snapshot)
    soc = snapshot.soc
    events: list[Event] = []
    grid = state.grid.observe(snapshot.grid_present, snapshot.taken_at, thresholds.confirm_polls)
    gen = state.gen.observe(snapshot.gen_running, snapshot.taken_at, thresholds.confirm_polls)
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

    if not grid.value and not gen.value:
        if soc <= thresholds.soc_critical and not new.critical_sent:
            events.append(Event(EventType.BATTERY_CRITICAL, snapshot.taken_at, soc))
            new = replace(new, critical_sent=True, low_sent=True)
        elif soc <= thresholds.soc_low and not new.low_sent:
            events.append(Event(EventType.BATTERY_LOW, snapshot.taken_at, soc))
            new = replace(new, low_sent=True)

    if gen.value and not grid.value and soc >= thresholds.soc_gen_full and not new.gen_charged_sent:
        events.append(Event(EventType.GEN_CHARGED, snapshot.taken_at, soc))
        new = replace(new, gen_charged_sent=True)

    return new, events
