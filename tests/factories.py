from datetime import UTC, datetime, timedelta

from lxbot.device.snapshot import Snapshot

BASE = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)


def at(poll: int) -> datetime:
    return BASE + timedelta(seconds=15 * poll)


def make_snapshot(poll: int = 0, *, grid: bool = True, gen: bool = False, soc: int = 80, **overrides) -> Snapshot:
    fields = dict(
        taken_at=at(poll),
        soc=soc,
        battery_w=0,
        pv_w=0,
        load_w=500,
        grid_v=230.0 if grid else 0.0,
        grid_hz=50.0 if grid else 0.0,
        gen_running=gen,
    )
    fields.update(overrides)
    return Snapshot(**fields)
