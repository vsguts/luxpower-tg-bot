from datetime import UTC, datetime, timedelta

from lxbot.luxpower.registers import Snapshot

BASE = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)


def at(poll: int) -> datetime:
    return BASE + timedelta(seconds=15 * poll)


def make_snapshot(poll: int = 0, *, grid: bool = True, gen: bool = False, soc: int = 80, **overrides) -> Snapshot:
    fields = dict(
        taken_at=at(poll),
        status=0x11 if grid or gen else 0x40,
        soc=soc,
        battery_v=52.0,
        battery_w=0,
        pv_w=0,
        grid_v=230.0 if grid else 0.0,
        grid_hz=50.0 if grid else 0.0,
        eps_v=230.0,
        eps_hz=50.0,
        eps_w=500,
        ac_source_gen=gen and not grid,
        gen_v=230.0 if gen else 0.0,
        gen_hz=50.0 if gen else 0.0,
        load_w=500,
    )
    fields.update(overrides)
    return Snapshot(**fields)
