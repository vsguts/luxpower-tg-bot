"""SNA input register map (function 0x04) and the decoded Snapshot.

The map comes from EG4 off-grid units (pylxpweb, luxpower-ha-integration) and is not yet verified
on the SNA6000 WPV: confirm new fields with tools/dump_registers.py against the inverter display.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

BLOCK_SIZE = 40
INPUT_BLOCKS = (0, 40, 120, 160)

MIN_AC_VOLTAGE = 180.0
# Hysteresis: a sagging feeder hovering around 180 V would otherwise flap between lost and restored.
GRID_HOLD_VOLTAGE = 170.0
MIN_AC_FREQUENCY = 45.0
# A 48 V pack never reads this low; such values mean the dongle sent an empty block.
MIN_BATTERY_VOLTAGE = 30.0

STATUS_NAMES = {
    0x00: "standby",
    0x04: "PV on-grid",
    0x08: "PV charge",
    0x0C: "PV charge on-grid",
    0x10: "battery on-grid",
    0x11: "bypass",
    0x14: "PV & battery on-grid",
    0x19: "PV charge + bypass",
    0x20: "AC charge",
    0x28: "PV & AC charge",
    0x40: "battery off-grid",
    0x80: "PV off-grid",
    0x88: "PV charge off-grid",
    0xC0: "PV & battery off-grid",
}
AC_INPUT_STATUSES = frozenset({0x11, 0x19, 0x20, 0x28})


class Reg(IntEnum):
    STATUS = 0
    BATTERY_V = 4
    SOC_SOH = 5
    PV1_W = 7
    PV2_W = 8
    PV3_W = 9
    CHARGE_W = 10
    DISCHARGE_W = 11
    GRID_V = 12
    GRID_HZ = 15
    EPS_V = 20
    EPS_HZ = 23
    EPS_W = 24
    AC_SOURCE = 77
    GEN_V = 121
    GEN_HZ = 122
    LOAD_W = 170


class InvalidSnapshot(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    status: int
    soc: int
    battery_v: float
    battery_w: int
    pv_w: int
    grid_v: float
    grid_hz: float
    eps_v: float
    eps_hz: float
    eps_w: int
    ac_source_gen: bool
    gen_v: float
    gen_hz: float
    load_w: int

    @property
    def grid_present(self) -> bool:
        return self.grid_present_given(currently_present=False)

    def grid_present_given(self, currently_present: bool) -> bool:
        min_voltage = GRID_HOLD_VOLTAGE if currently_present else MIN_AC_VOLTAGE
        return self.grid_v > min_voltage and self.grid_hz > MIN_AC_FREQUENCY

    @property
    def ac_input_present(self) -> bool:
        return self.status in AC_INPUT_STATUSES

    @property
    def gen_running(self) -> bool:
        gen_voltage_ok = self.gen_v > MIN_AC_VOLTAGE and self.gen_hz > MIN_AC_FREQUENCY
        return gen_voltage_ok or (self.ac_source_gen and self.ac_input_present)

    @property
    def status_name(self) -> str:
        return STATUS_NAMES.get(self.status, f"unknown {self.status:#04x}")


def _signed(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def decode_snapshot(registers: Mapping[int, int], taken_at: datetime) -> Snapshot:
    r = registers
    snapshot = Snapshot(
        taken_at=taken_at,
        status=r[Reg.STATUS],
        soc=r[Reg.SOC_SOH] & 0xFF,
        battery_v=r[Reg.BATTERY_V] / 10,
        battery_w=r[Reg.CHARGE_W] - r[Reg.DISCHARGE_W],
        pv_w=r[Reg.PV1_W] + r[Reg.PV2_W] + r[Reg.PV3_W],
        grid_v=r[Reg.GRID_V] / 10,
        grid_hz=r[Reg.GRID_HZ] / 100,
        eps_v=r[Reg.EPS_V] / 10,
        eps_hz=r[Reg.EPS_HZ] / 100,
        eps_w=r[Reg.EPS_W],
        ac_source_gen=bool(r[Reg.AC_SOURCE] & 1),
        gen_v=r[Reg.GEN_V] / 10,
        gen_hz=r[Reg.GEN_HZ] / 100,
        load_w=_signed(r[Reg.LOAD_W]),
    )
    if snapshot.battery_v < MIN_BATTERY_VOLTAGE or snapshot.soc > 100:
        raise InvalidSnapshot(f"implausible battery {snapshot.battery_v} V, SOC {snapshot.soc}%")
    return snapshot
