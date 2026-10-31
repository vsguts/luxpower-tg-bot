import json
from pathlib import Path

import pytest

from lxbot.device.snapshot import Snapshot
from lxbot.ecoflow.quota import InvalidQuota, decode_quota
from tests.factories import BASE

GRID_ON = json.loads((Path(__file__).parent / "fixtures" / "ecoflow-delta-pro-grid-on.json").read_text())


def _quota(**overrides: object) -> dict[str, object]:
    return GRID_ON | {key.replace("__", "."): value for key, value in overrides.items()}


def test_decode_the_grid_on_sample():
    assert decode_quota(GRID_ON, BASE) == Snapshot(
        taken_at=BASE, soc=100, battery_w=0, pv_w=0, load_w=0, grid_v=217.174, grid_hz=50, gen_running=False
    )
    assert decode_quota(GRID_ON, BASE).grid_present


def test_grid_off_reads_no_ac_input():
    snapshot = decode_quota(_quota(inv__acInVol=0, inv__acInFreq=0), BASE)
    assert not snapshot.grid_present


def test_discharge_is_negative_and_charge_positive():
    assert decode_quota(_quota(bmsMaster__inputWatts=0, bmsMaster__outputWatts=502), BASE).battery_w == -502
    assert decode_quota(_quota(bmsMaster__inputWatts=1200, bmsMaster__outputWatts=0), BASE).battery_w == 1200


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
