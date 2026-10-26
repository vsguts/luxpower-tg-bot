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


def test_low_battery_needs_a_confirmed_low_charge():
    assert variables(make_snapshot(grid=False, soc=5), state(grid=False))["ups.status"] == "OB"
    ups = ups_variables(make_snapshot(grid=False, soc=6), state(grid=False), at(0), LOW, BATTERY, low_confirmed=True)
    assert ups["ups.status"] == "OB LB"


def test_a_low_charge_on_line_is_not_low_battery():
    ups = ups_variables(make_snapshot(soc=3), state(), at(0), LOW, BATTERY, low_confirmed=True)
    assert ups["ups.status"] == "OL"


def lost(snapshot, current, after, battery=BATTERY, **flags):
    return ups_variables(snapshot, current, snapshot.taken_at + after, LOW, battery, **flags)["ups.status"]


def test_a_dongle_lost_near_the_threshold_means_the_inverter_shut_down():
    snapshot = make_snapshot(grid=False, soc=8, battery_w=-500)
    assert lost(snapshot, state(grid=False), timedelta(minutes=3)) == "OB LB"


def test_a_brief_gap_near_the_threshold_is_not_a_shutdown():
    snapshot = make_snapshot(grid=False, soc=7, battery_w=-500)
    assert lost(snapshot, state(grid=False), timedelta(seconds=60)) == "OB"


def test_a_dongle_lost_higher_up_waits_for_the_estimated_charge():
    # 20 % at 3 kW: 14 % of 16 kWh above the threshold lasts about 45 min.
    snapshot = make_snapshot(grid=False, soc=20, battery_w=-3000)
    assert lost(snapshot, state(grid=False), timedelta(minutes=40)) == "OB"
    assert lost(snapshot, state(grid=False), timedelta(minutes=46)) == "OB LB"


def test_a_wifi_gap_on_a_healthy_battery_keeps_the_last_reading():
    snapshot = make_snapshot(grid=False, soc=60, battery_w=-500)
    ups = ups_variables(snapshot, state(grid=False), snapshot.taken_at + timedelta(hours=1), LOW, BATTERY)
    assert ups["ups.status"] == "OB"
    assert ups["battery.charge"] == "60"
    # Readings that are this old say nothing about now.
    assert "input.voltage" not in ups
    assert "battery.runtime" not in ups


def test_without_a_capacity_only_the_near_threshold_rule_applies():
    snapshot = make_snapshot(grid=False, soc=20, battery_w=-3000)
    assert lost(snapshot, state(grid=False), timedelta(hours=10), battery=None) == "OB"


def test_an_inverter_that_died_while_the_grid_loss_was_debounced():
    # The grid went at 6 %; the inverter shut down two polls later, before the loss was confirmed.
    snapshot = make_snapshot(grid=False, soc=6, battery_w=-500)
    assert lost(snapshot, state(grid=True), timedelta(minutes=3)) == "OB LB"


def test_an_inverter_that_died_when_the_generator_stopped():
    snapshot = make_snapshot(grid=False, gen=False, soc=5, battery_w=-500)
    assert lost(snapshot, state(grid=False, gen=True), timedelta(minutes=3)) == "OB LB"


def test_a_restart_with_the_dongle_gone_after_the_critical_alert():
    critical = State(grid=Debounced(False, at(0)), gen=Debounced(False, at(0)), critical_sent=True)
    assert ups_variables(None, critical, at(0), LOW, BATTERY)["ups.status"] == "OB"
    ups = ups_variables(None, critical, at(0), LOW, BATTERY, reading_overdue=True)
    assert ups["ups.status"] == "OB LB"
    assert ups_variables(None, state(grid=False), at(0), LOW, BATTERY, reading_overdue=True)["ups.status"] == "OB"


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
