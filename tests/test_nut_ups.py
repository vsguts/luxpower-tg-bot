from types import SimpleNamespace

from lxbot.monitor.detector import initial_state
from lxbot.nut.server import UpsConfig
from lxbot.nut.ups import InverterUps
from tests.factories import make_snapshot

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
