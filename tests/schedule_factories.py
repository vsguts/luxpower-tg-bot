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
