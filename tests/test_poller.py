from datetime import timedelta

from lxbot.device.snapshot import DeviceError
from lxbot.monitor.detector import EventType, Thresholds
from lxbot.monitor.poller import STATE_KEY, Poller
from tests.factories import at, make_snapshot


class ScriptedSource:
    def __init__(self, script):
        self.script = list(script)

    async def read_snapshot(self, taken_at):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingSink:
    def __init__(self):
        self.events = []

    async def publish(self, events):
        self.events.extend(events)


class Clock:
    def __init__(self, times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0)


def make_poller(script, db, sink, times=None):
    times = times if times is not None else [s.taken_at for s in script]
    return Poller(ScriptedSource(script), db, sink, Thresholds(), interval=0, clock=Clock(times))


async def poll_all(poller, count):
    await poller.load()
    for _ in range(count):
        await poller.poll_once()


async def test_outage_is_published_and_state_saved(db):
    script = [make_snapshot(0)] + [make_snapshot(i, grid=False) for i in (1, 2, 3)]
    sink = RecordingSink()
    poller = make_poller(script, db, sink)
    await poll_all(poller, 4)
    assert [e.type for e in sink.events] == [EventType.GRID_LOST]
    assert poller.latest.taken_at == at(3)
    assert (await db.load_state(STATE_KEY))["grid"]["value"] is False


async def test_restart_mid_outage_keeps_outage_start(db):
    first = [make_snapshot(0)] + [make_snapshot(i, grid=False) for i in (1, 2, 3)]
    sink = RecordingSink()
    await poll_all(make_poller(first, db, sink), 4)

    second = [make_snapshot(i, grid=False) for i in (40, 41)] + [make_snapshot(i) for i in (42, 43, 44)]
    await poll_all(make_poller(second, db, sink), 5)

    assert [e.type for e in sink.events] == [EventType.GRID_LOST, EventType.GRID_RESTORED]
    assert sink.events[1].previous_duration == at(42) - at(1)


async def test_dongle_down_and_up_reach_the_sink(db):
    failures = [DeviceError("timeout")] * 3
    times = [at(0), at(0) + timedelta(minutes=3), at(0) + timedelta(minutes=5), at(30)]
    sink = RecordingSink()
    poller = make_poller(failures + [make_snapshot(30)], db, sink, times)
    await poll_all(poller, 4)
    assert [e.type for e in sink.events] == [EventType.DEVICE_DOWN, EventType.DEVICE_UP]
    assert sink.events[0].at == at(0)


async def test_state_is_written_only_when_it_changes(db, monkeypatch):
    writes = []
    save_state = db.save_state

    async def counting_save_state(key, value):
        writes.append(value)
        await save_state(key, value)

    monkeypatch.setattr(db, "save_state", counting_save_state)
    script = [make_snapshot(i) for i in range(5)]
    await poll_all(make_poller(script, db, RecordingSink()), 5)
    assert len(writes) == 1


async def test_run_survives_an_unexpected_error(db):
    class BrokenOnceSource:
        calls = 0

        async def read_snapshot(self, taken_at):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bug")
            if self.calls == 3:
                raise SystemExit  # stops run() for the test
            return make_snapshot(0)

    source = BrokenOnceSource()
    poller = Poller(source, db, RecordingSink(), Thresholds(), interval=0)
    try:
        await poller.run()
    except SystemExit:
        pass
    assert source.calls == 3
    assert poller.latest is not None


async def test_run_leaves_loading_to_the_caller(db):
    # main loads before starting the task, so a broken stored state crashes the service visibly.
    class StoppingSource:
        async def read_snapshot(self, taken_at):
            raise SystemExit

    await db.save_state(STATE_KEY, {"broken": True})
    poller = Poller(StoppingSource(), db, RecordingSink(), Thresholds(), interval=0)
    try:
        await poller.run()
    except SystemExit:
        pass
    assert poller.state is None
