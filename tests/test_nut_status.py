from datetime import timedelta

from lxbot.monitor.detector import Debounced, State
from lxbot.monitor.forecast import BatteryConfig
from lxbot.nut.status import ups_variables
from tests.factories import at, make_snapshot

LOW = 6
BATTERY = BatteryConfig(16000, 5)


def state(grid=True, gen=False):
    return State(grid=Debounced(grid, at(0)), gen=Debounced(gen, at(0)))


def variables(snapshot, current=None, now=None, battery=BATTERY, test_low=False):
    now = now or (snapshot.taken_at if snapshot else at(0))
    return ups_variables(snapshot, current or state(), now, LOW, battery, test_low=test_low)


def test_grid_present_is_on_line():
    ups = variables(make_snapshot(soc=80))
    assert ups["ups.status"] == "OL"
    assert ups["battery.charge"] == "80"
    assert ups["battery.charge.low"] == "6"
    assert ups["input.voltage"] == "230"
    assert ups["ups.realpower"] == "500"
    assert "battery.runtime" not in ups


def test_charging_is_flagged():
    assert variables(make_snapshot(battery_w=3900))["ups.status"] == "OL CHRG"


def test_on_battery_with_a_runtime_estimate():
    ups = variables(make_snapshot(grid=False, soc=50, battery_w=-1000, load_w=1000), state(grid=False))
    assert ups["ups.status"] == "OB"
    # (50 - 5) % of 16 kWh at 1 kW is 7 h 12 min, rounded like every forecast to 10 min.
    assert ups["battery.runtime"] == str(int(timedelta(hours=7, minutes=10).total_seconds()))


def test_without_a_capacity_there_is_no_runtime():
    ups = variables(make_snapshot(grid=False, battery_w=-1000), state(grid=False), battery=None)
    assert "battery.runtime" not in ups


def test_the_generator_counts_as_line_power():
    ups = variables(make_snapshot(grid=False, gen=True, soc=4), state(grid=False, gen=True))
    assert ups["ups.status"] == "OL"


def test_low_battery_at_the_threshold():
    assert variables(make_snapshot(grid=False, soc=7), state(grid=False))["ups.status"] == "OB"
    assert variables(make_snapshot(grid=False, soc=6), state(grid=False))["ups.status"] == "OB LB"


def test_a_low_charge_on_line_is_not_low_battery():
    assert variables(make_snapshot(soc=3))["ups.status"] == "OL"


def test_a_dongle_lost_on_a_low_battery_means_the_inverter_shut_down():
    snapshot = make_snapshot(grid=False, soc=15)
    later = snapshot.taken_at + timedelta(minutes=3)
    assert variables(snapshot, state(grid=False), now=later)["ups.status"] == "OB LB"


def test_a_dongle_lost_on_a_healthy_battery_keeps_the_last_reading():
    snapshot = make_snapshot(grid=False, soc=60)
    later = snapshot.taken_at + timedelta(hours=1)
    ups = variables(snapshot, state(grid=False), now=later)
    assert ups["ups.status"] == "OB"
    assert ups["battery.charge"] == "60"
    # Readings that are this old say nothing about now.
    assert "input.voltage" not in ups
    assert "battery.runtime" not in ups


def test_a_brief_gap_on_a_low_battery_is_not_a_shutdown():
    snapshot = make_snapshot(grid=False, soc=12)
    soon = snapshot.taken_at + timedelta(seconds=60)
    assert variables(snapshot, state(grid=False), now=soon)["ups.status"] == "OB"


def test_the_admin_test_reports_a_low_battery_whatever_the_reading():
    ups = variables(make_snapshot(soc=90), test_low=True)
    assert ups["ups.status"] == "OB LB"
    assert ups["battery.charge"] == "90"


def test_before_the_first_reading_the_stored_state_decides():
    ups = ups_variables(None, state(grid=False), at(0), LOW, BATTERY)
    assert ups["ups.status"] == "OB"
    assert "battery.charge" not in ups


def test_with_no_state_at_all_there_is_no_data():
    assert ups_variables(None, None, at(0), LOW, BATTERY) is None


def test_the_device_is_described():
    ups = variables(make_snapshot())
    assert (ups["device.mfr"], ups["device.model"], ups["device.type"]) == ("LuxPower", "SNA6000 WPV", "ups")
    assert (ups["ups.mfr"], ups["ups.model"]) == ("LuxPower", "SNA6000 WPV")
