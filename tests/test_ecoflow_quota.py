import json
from pathlib import Path

import pytest

from lxbot.device.snapshot import Snapshot
from lxbot.ecoflow.quota import InvalidQuota, decode_quota
from tests.factories import BASE

FIXTURES = Path(__file__).parent / "fixtures"
GRID_ON = json.loads((FIXTURES / "ecoflow-delta-pro-grid-on.json").read_text())
# Taken during a real outage: the BMS watts read 0 while the battery gives about 118 W.
GRID_OFF = json.loads((FIXTURES / "ecoflow-delta-pro-grid-off.json").read_text())


def _quota(**overrides: object) -> dict[str, object]:
    return GRID_ON | {key.replace("__", "."): value for key, value in overrides.items()}


def test_decode_the_grid_on_sample():
    assert decode_quota(GRID_ON, BASE) == Snapshot(
        taken_at=BASE, soc=100, battery_w=0, pv_w=0, load_w=0, grid_v=217.174, grid_hz=50, gen_running=False
    )
    assert decode_quota(GRID_ON, BASE).grid_present


def test_decode_the_grid_off_sample():
    assert decode_quota(GRID_OFF, BASE) == Snapshot(
        taken_at=BASE, soc=98, battery_w=-118, pv_w=0, load_w=50, grid_v=0.0, grid_hz=0, gen_running=False
    )
    assert not decode_quota(GRID_OFF, BASE).grid_present


def test_battery_power_comes_from_current_and_voltage():
    # mA × mV: 10 A at 50 V charges with 500 W, -2.4 A discharges with 120 W.
    assert decode_quota(_quota(bmsMaster__amp=10_000, bmsMaster__vol=50_000), BASE).battery_w == 500
    assert decode_quota(_quota(bmsMaster__amp=-2_400, bmsMaster__vol=50_000), BASE).battery_w == -120


@pytest.mark.parametrize("milliamps", [-135, 135, -180])
def test_the_bms_idle_current_is_no_flow(milliamps):
    assert decode_quota(_quota(bmsMaster__amp=milliamps, bmsMaster__vol=50_122), BASE).battery_w == 0


def test_load_and_solar():
    snapshot = decode_quota(_quota(pd__wattsOutSum=467, mppt__inWatts=310), BASE)
    assert (snapshot.load_w, snapshot.pv_w) == (467, 310)


def test_missing_field_is_invalid():
    quota = dict(GRID_ON)
    del quota["pd.soc"]
    with pytest.raises(InvalidQuota, match="pd.soc"):
        decode_quota(quota, BASE)


@pytest.mark.parametrize("value", [None, "100", True])
def test_non_number_is_invalid(value):
    with pytest.raises(InvalidQuota, match="pd.soc"):
        decode_quota(_quota(pd__soc=value), BASE)


def test_soc_above_100_is_invalid():
    with pytest.raises(InvalidQuota, match="SOC"):
        decode_quota(_quota(pd__soc=101), BASE)


def test_a_flow_of_ten_watts_is_reported():
    assert decode_quota(_quota(bmsMaster__amp=-200, bmsMaster__vol=50_000), BASE).battery_w == -10
