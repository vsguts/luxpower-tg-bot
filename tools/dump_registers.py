"""Print every input register once, raw and decoded, to check the register map on this inverter.

Stop the bot first: the dongle serves one local client at a time.
"""

import asyncio
import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime

from lxbot.config import dongle_config_from_env
from lxbot.luxpower.client import DongleClient
from lxbot.luxpower.registers import BLOCK_SIZE, InvalidReading, decode_reading

DUMP_BLOCKS = (0, 40, 80, 120, 160)


async def dump(env: Mapping[str, str]) -> None:
    client = DongleClient(dongle_config_from_env(env))
    registers: dict[int, int] = {}
    try:
        for start in DUMP_BLOCKS:
            registers.update(enumerate(await client.read_input(start, BLOCK_SIZE), start))
    finally:
        await client.close()

    print("Raw input registers (non-zero): reg, value, hex")
    for reg, value in sorted(registers.items()):
        if value:
            print(f"  {reg:3d}  {value:6d}  {value:#06x}")

    try:
        reading = decode_reading(registers, datetime.now(UTC))
    except InvalidReading as exc:
        print(f"\nCannot decode: {exc}")
        return
    print("\nDecoded:")
    for name, value in asdict(reading).items():
        print(f"  {name:14s} {value}")
    print(f"  {'status_name':14s} {reading.status_name}")
    print(f"  {'grid_present':14s} {reading.to_snapshot().grid_present}")
    print(f"  {'gen_running':14s} {reading.gen_running}")


if __name__ == "__main__":
    asyncio.run(dump(os.environ))
