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
