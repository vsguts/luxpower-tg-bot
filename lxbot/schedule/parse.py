"""outage-data-ua document (github.com/Baskerville42/outage-data-ua) -> Schedule for one group."""

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
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
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError) as exc:
        raise ScheduleParseError(f"malformed schedule document: {exc!r}") from exc
    if not days:
        raise ScheduleParseError(f"group {group} is not in the schedule document")
    status = document.get("lastUpdateStatus")
    if not isinstance(status, Mapping):
        status = {}
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
        # Each boundary is a wall-clock reading converted on its own: adding minutes to an aware
        # datetime counts elapsed time and splits adjacent slots around a DST switch. time() also
        # rejects slots outside 1..24.
        hour_start = datetime.combine(day, time(int(slot) - 1))
        outages.append(
            Outage(
                _wall_to_utc(hour_start + timedelta(minutes=first), tz),
                _wall_to_utc(hour_start + timedelta(minutes=last), tz),
                certain,
            )
        )
    return outages


def _wall_to_utc(wall: datetime, tz: ZoneInfo) -> datetime:
    return wall.replace(tzinfo=tz).astimezone(UTC)


def _dtek_time(value: object, tz: ZoneInfo) -> datetime | None:
    try:
        return datetime.strptime(value, _DTEK_TIME_FORMAT).replace(tzinfo=tz)
    except (TypeError, ValueError):
        return None


def _utc_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
