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
        # DTEK publishes no days at all while no outages are planned (most of the summer).
        if not self.days:
            return DaySchedule(self.group, day)
        return self.days.get(day)

    # Merged across midnight, so "23:00-01:00" is one interval for matching.
    def timeline(self) -> tuple[Outage, ...]:
        return merge_outages(o for d in self.days.values() for o in d.outages)


@dataclass(frozen=True)
class ScheduleUpdate:
    """One message about the schedule: every known day from today on, and which of them are news."""

    days: tuple[DaySchedule, ...]
    published: tuple[date, ...] = ()
    changed: tuple[date, ...] = ()

    def to_dict(self) -> dict:
        return {
            "days": [d.to_dict() for d in self.days],
            "published": [d.isoformat() for d in self.published],
            "changed": [d.isoformat() for d in self.changed],
        }
