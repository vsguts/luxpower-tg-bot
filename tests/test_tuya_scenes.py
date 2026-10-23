import asyncio
from datetime import timedelta

from lxbot.monitor.detector import Audience, Event, EventType
from lxbot.tuya.api import TuyaError
from lxbot.tuya.scenes import SceneTrigger
from tests.factories import at

LOST_SCENE = "sceneLost01"
SCENES = {EventType.GRID_LOST: LOST_SCENE}
NOW = at(10)


class Runner:
    def __init__(self, *failures):
        self.failures = list(failures)
        self.calls = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def trigger_scene(self, rule_id):
        self.calls.append(rule_id)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.failures:
            raise self.failures.pop(0)

    async def reached(self, count):
        while len(self.calls) < count:
            await asyncio.sleep(0.005)


class Sink:
    def __init__(self):
        self.events = []

    async def publish(self, events):
        self.events.extend(events)


def make_trigger(runner, scenes=SCENES):
    sink = Sink()
    return SceneTrigger(sink, runner, scenes, retry_delays=(0, 0), clock=lambda: NOW), sink


def grid_lost():
    return Event(EventType.GRID_LOST, at(3), soc=80)


async def test_the_grid_alert_is_handed_on_before_the_scene_runs():
    runner = Runner()
    runner.release = asyncio.Event()
    trigger, sink = make_trigger(runner)
    await trigger.publish([grid_lost()])
    await runner.started.wait()
    assert [e.type for e in sink.events] == [EventType.GRID_LOST]
    runner.release.set()
    await trigger.close()
    assert [e.type for e in sink.events] == [EventType.GRID_LOST, EventType.TUYA_SCENE_RUN]


async def test_a_lost_grid_runs_its_scene_and_journals_it():
    runner = Runner()
    trigger, sink = make_trigger(runner)
    await trigger.publish([grid_lost()])
    await trigger.close()
    assert runner.calls == [LOST_SCENE]
    run = sink.events[-1]
    assert (run.type, run.at, run.cause, run.detail) == (EventType.TUYA_SCENE_RUN, NOW, EventType.GRID_LOST, LOST_SCENE)
    assert run.audience == Audience.JOURNAL


async def test_events_without_a_scene_run_nothing():
    runner = Runner()
    trigger, sink = make_trigger(runner)
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW), Event(EventType.BATTERY_LOW, NOW, soc=30)])
    await trigger.close()
    assert runner.calls == []
    assert [e.type for e in sink.events] == [EventType.GRID_RESTORED, EventType.BATTERY_LOW]


async def test_a_restored_grid_runs_its_scene_when_one_is_set():
    runner = Runner()
    trigger, _ = make_trigger(runner, SCENES | {EventType.GRID_RESTORED: "sceneBack01"})
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW)])
    await trigger.close()
    assert runner.calls == ["sceneBack01"]


async def test_a_failed_attempt_is_retried():
    runner = Runner(TuyaError("blip"))
    trigger, sink = make_trigger(runner)
    await trigger.publish([grid_lost()])
    await trigger.close()
    assert runner.calls == [LOST_SCENE, LOST_SCENE]
    assert sink.events[-1].type == EventType.TUYA_SCENE_RUN


async def test_the_admin_hears_when_every_attempt_failed():
    runner = Runner(TuyaError("a"), TuyaError("b"), TuyaError("1106 permission deny"))
    trigger, sink = make_trigger(runner)
    await trigger.publish([grid_lost()])
    await trigger.close()
    assert len(runner.calls) == 3
    failed = sink.events[-1]
    assert (failed.type, failed.cause, failed.detail) == (EventType.TUYA_SCENE_FAILED, EventType.GRID_LOST, "1106 permission deny")
    assert failed.audience == Audience.ADMIN


async def test_an_unexpected_error_is_reported_too():
    runner = Runner(RuntimeError("bug"), RuntimeError("bug"), RuntimeError("bug"))
    trigger, sink = make_trigger(runner)
    await trigger.publish([grid_lost()])
    await trigger.close()
    assert sink.events[-1].type == EventType.TUYA_SCENE_FAILED


async def test_publish_does_not_wait_for_the_cloud():
    runner = Runner()
    runner.release = asyncio.Event()
    trigger, _ = make_trigger(runner)
    await asyncio.wait_for(trigger.publish([grid_lost()]), timeout=1)
    runner.release.set()
    await trigger.close()


async def test_close_cancels_a_scene_stuck_past_the_grace(monkeypatch):
    monkeypatch.setattr("lxbot.tuya.scenes.CLOSE_GRACE", 0.01)
    runner = Runner()
    runner.release = asyncio.Event()
    trigger, sink = make_trigger(runner)
    await trigger.publish([grid_lost()])
    await runner.started.wait()
    await asyncio.wait_for(trigger.close(), timeout=1)
    assert [e.type for e in sink.events] == [EventType.GRID_LOST]


async def test_a_restored_grid_stops_a_lost_scene_still_retrying():
    runner = Runner(TuyaError("blip"))
    sink = Sink()
    trigger = SceneTrigger(
        sink, runner, SCENES | {EventType.GRID_RESTORED: "sceneBack01"}, retry_delays=(0.2,), clock=lambda: NOW
    )
    await trigger.publish([grid_lost()])
    await runner.started.wait()
    await asyncio.sleep(0.05)
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW)])
    await trigger.close()
    await asyncio.sleep(0.3)
    assert runner.calls == [LOST_SCENE, "sceneBack01"]


async def test_a_restored_grid_without_a_scene_still_stops_the_lost_one():
    runner = Runner(TuyaError("blip"))
    sink = Sink()
    trigger = SceneTrigger(sink, runner, SCENES, retry_delays=(0.2,), clock=lambda: NOW)
    await trigger.publish([grid_lost()])
    await runner.started.wait()
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW)])
    await asyncio.sleep(0.3)
    assert runner.calls == [LOST_SCENE]
    assert EventType.TUYA_SCENE_FAILED not in [e.type for e in sink.events]


async def test_a_journal_that_fails_still_runs_the_scene():
    class BrokenSink:
        async def publish(self, events):
            raise OSError("disk full")

    runner = Runner()
    trigger = SceneTrigger(BrokenSink(), runner, SCENES, retry_delays=(), clock=lambda: NOW)
    try:
        await trigger.publish([grid_lost()])
    except OSError:
        pass
    await trigger.close()
    assert runner.calls == [LOST_SCENE]


REPEAT = timedelta(seconds=0.02)
# Generous: the checks wait for calls to happen, not for a fixed time, so a slow machine only waits longer.
WAIT = 5


class Grid:
    def __init__(self):
        self.out = True

    def __call__(self):
        return self.out


def repeating(runner, scenes=SCENES, grid=None):
    sink = Sink()
    trigger = SceneTrigger(
        sink, runner, scenes, retry_delays=(0, 0), repeat=REPEAT, grid_out=grid or Grid(), clock=lambda: NOW
    )
    return trigger, sink


def types(sink):
    return [e.type for e in sink.events]


async def calls_stay(runner, pause=0.1):
    calls = len(runner.calls)
    await asyncio.sleep(pause)
    return len(runner.calls) == calls


async def test_the_lost_scene_repeats_while_the_grid_is_out():
    runner = Runner()
    trigger, sink = repeating(runner)
    await trigger.publish([grid_lost()])
    await asyncio.wait_for(runner.reached(4), WAIT)
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW)])
    assert await calls_stay(runner)
    # The card is written once per outage, not on every repeat.
    assert types(sink).count(EventType.TUYA_SCENE_RUN) == 1
    await trigger.close()


async def test_no_repeat_while_the_return_of_the_grid_is_debounced():
    runner, grid = Runner(), Grid()
    trigger, _ = repeating(runner, grid=grid)
    await trigger.publish([grid_lost()])
    await asyncio.wait_for(runner.reached(2), WAIT)
    grid.out = False
    await asyncio.sleep(0.03)
    assert await calls_stay(runner)
    grid.out = True
    await asyncio.wait_for(runner.reached(len(runner.calls) + 1), WAIT)
    await trigger.close()


async def test_the_restored_scene_does_not_repeat():
    runner = Runner()
    trigger, _ = repeating(runner, SCENES | {EventType.GRID_RESTORED: "sceneBack01"})
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW)])
    await asyncio.wait_for(runner.reached(1), WAIT)
    assert await calls_stay(runner)
    assert runner.calls == ["sceneBack01"]
    await trigger.close()


async def test_failed_repeats_tell_the_admin_once_per_outage():
    runner = Runner(*[TuyaError("down")] * 100)
    trigger, sink = repeating(runner)
    await trigger.publish([grid_lost()])
    await asyncio.wait_for(runner.reached(8), WAIT)
    assert types(sink).count(EventType.TUYA_SCENE_FAILED) == 1
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW)])
    await trigger.publish([grid_lost()])
    await asyncio.wait_for(runner.reached(len(runner.calls) + 3), WAIT)
    assert types(sink).count(EventType.TUYA_SCENE_FAILED) == 2
    await trigger.close()


async def test_one_failed_repeat_is_a_blip_two_are_reported():
    runner = Runner()
    trigger, sink = repeating(runner)
    await trigger.publish([grid_lost()])
    await asyncio.wait_for(runner.reached(1), WAIT)
    runner.failures = [TuyaError("blip")]
    await asyncio.wait_for(runner.reached(4), WAIT)
    assert EventType.TUYA_SCENE_FAILED not in types(sink)
    runner.failures = [TuyaError("down")] * 100
    await asyncio.wait_for(runner.reached(len(runner.calls) + 3), WAIT)
    failed = [e for e in sink.events if e.type == EventType.TUYA_SCENE_FAILED]
    assert [(e.cause, e.detail) for e in failed] == [(EventType.GRID_LOST, "down")]
    await trigger.close()


async def test_an_outage_found_on_start_resumes_the_repeats_only():
    runner = Runner()
    trigger, sink = repeating(runner)
    trigger.resume_outage()
    # The scene ran before the restart: nothing at once, nothing journaled.
    assert runner.calls == []
    await asyncio.wait_for(runner.reached(2), WAIT)
    assert sink.events == []
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW)])
    assert await calls_stay(runner)
    await trigger.close()


async def test_an_outage_found_on_start_with_the_grid_back_runs_nothing():
    runner, grid = Runner(), Grid()
    grid.out = False
    trigger, _ = repeating(runner, grid=grid)
    trigger.resume_outage()
    assert await calls_stay(runner)
    assert runner.calls == []
    await trigger.close()


async def test_without_a_repeat_the_scene_runs_once():
    runner = Runner()
    trigger, _ = make_trigger(runner)
    await trigger.publish([grid_lost()])
    await asyncio.wait_for(runner.reached(1), WAIT)
    assert await calls_stay(runner)
    trigger.resume_outage()
    assert await calls_stay(runner)
    assert runner.calls == [LOST_SCENE]
    await trigger.close()


async def test_close_stops_the_repeats_at_once():
    runner = Runner()
    trigger, _ = repeating(runner)
    await trigger.publish([grid_lost()])
    await asyncio.wait_for(runner.reached(2), WAIT)
    await asyncio.wait_for(trigger.close(), timeout=1)
    assert await calls_stay(runner)


async def test_a_repeat_loop_that_survives_its_cancellation_ends_itself():
    class SwallowsCancel(Runner):
        async def trigger_scene(self, rule_id):
            self.calls.append(rule_id)
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                # What aiohttp can do: a cancelled request surfaces as a timeout.
                raise TuyaError("TimeoutError") from None

    runner = SwallowsCancel()
    trigger, _ = repeating(runner)
    trigger.resume_outage()
    await asyncio.wait_for(runner.reached(1), WAIT)
    await trigger.publish([Event(EventType.GRID_RESTORED, NOW)])
    assert await calls_stay(runner, pause=0.2)
    await trigger.close()
