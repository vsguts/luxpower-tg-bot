from types import SimpleNamespace

from lxbot.monitor.detector import Debounced, State, initial_state
from lxbot.nut.server import UpsConfig
from lxbot.nut.ups import InverterUps
from tests.factories import at, make_snapshot

CONFIG = UpsConfig(allowed_hosts=frozenset({"192.168.110.110"}), low_soc=6)


def test_the_ups_reads_the_poller_live():
    snapshot = make_snapshot(0, soc=80)
    poller = SimpleNamespace(latest=snapshot, state=initial_state(snapshot))
    ups = InverterUps(poller, CONFIG, None, clock=lambda: snapshot.taken_at)
    assert ups.variables()["battery.charge"] == "80"
    poller.latest = make_snapshot(1, soc=79)
    assert ups.variables()["battery.charge"] == "79"


def test_the_admin_test_overrides_the_status():
    snapshot = make_snapshot(0, soc=80)
    poller = SimpleNamespace(latest=snapshot, state=initial_state(snapshot))
    ups = InverterUps(poller, CONFIG, None, clock=lambda: snapshot.taken_at)
    ups.test_low = True
    assert ups.variables()["ups.status"] == "OB LB"
    ups.test_low = False
    assert ups.variables()["ups.status"] == "OL"


def test_no_state_yet_means_no_data():
    ups = InverterUps(SimpleNamespace(latest=None, state=None), CONFIG, None)
    assert ups.variables() is None


class Poller:
    def __init__(self):
        self.latest = None
        self.state = State(grid=Debounced(False, at(0)), gen=Debounced(False, at(0)))
        self.poll = 0

    def read(self, soc, **flags):
        self.poll += 1
        self.latest = make_snapshot(self.poll, grid=False, soc=soc, battery_w=-500, **flags)


def outage_ups(poller):
    return InverterUps(
        poller, CONFIG, None, confirm_polls=2, clock=lambda: poller.latest.taken_at if poller.latest else at(0)
    )


def statuses(poller, ups, charges):
    result = []
    for soc in charges:
        poller.read(soc)
        result.append(ups.variables()["ups.status"])
    return result


def test_one_garbled_reading_does_not_report_a_low_battery():
    poller = Poller()
    ups = outage_ups(poller)
    assert statuses(poller, ups, [60, 60, 0, 60]) == ["OB", "OB", "OB", "OB"]


def test_two_low_readings_in_a_row_do():
    poller = Poller()
    ups = outage_ups(poller)
    assert statuses(poller, ups, [7, 6, 6, 5]) == ["OB", "OB", "OB LB", "OB LB"]


def test_the_same_reading_twice_counts_once():
    poller = Poller()
    ups = outage_ups(poller)
    poller.read(6)
    assert ups.variables()["ups.status"] == "OB"
    assert ups.variables()["ups.status"] == "OB"


def test_a_low_battery_holds_through_jitter_and_clears_two_points_up():
    poller = Poller()
    ups = outage_ups(poller)
    assert statuses(poller, ups, [6, 6, 7, 6, 7, 8]) == ["OB", "OB LB", "OB LB", "OB LB", "OB LB", "OB"]


def test_the_grid_coming_back_clears_a_low_battery():
    poller = Poller()
    ups = outage_ups(poller)
    statuses(poller, ups, [6, 6])
    poller.state = State(grid=Debounced(True, at(9)), gen=Debounced(False, at(0)))
    poller.read(6)
    assert ups.variables()["ups.status"].startswith("OL")
    poller.state = State(grid=Debounced(False, at(20)), gen=Debounced(False, at(0)))
    assert statuses(poller, ups, [6]) == ["OB"]
