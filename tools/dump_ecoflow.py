"""Print the EcoFlow cloud's quota for one station as JSON, to map its fields onto a snapshot.

The device state goes to stderr, so stdout is a clean JSON file:
    docker compose run --rm -T bot python -m tools.dump_ecoflow > delta-pro.json
"""

import asyncio
import json
import os
import sys
from collections.abc import Mapping

from lxbot.config import ConfigError, ecoflow_config_from_env
from lxbot.ecoflow.api import EcoFlowApi, EcoFlowError


async def dump(env: Mapping[str, str]) -> int:
    config = ecoflow_config_from_env(env)
    api = EcoFlowApi(config)
    try:
        devices = await api.devices()
        device = next((d for d in devices if d.get("sn") == config.serial), None)
        if device is None:
            bound = ", ".join(f"{d.get('sn')} ({d.get('productName')})" for d in devices) or "none"
            print(f"{config.serial} is not bound to this account. Bound devices: {bound}", file=sys.stderr)
            return 1
        quota = await api.quota()
    finally:
        await api.close()

    # The cloud keeps answering with the last values it saw from an offline station.
    state = "online" if device.get("online") == 1 else "OFFLINE, the values are the last ones the cloud saw"
    print(f"{device.get('productName')}: {state}", file=sys.stderr)
    print(json.dumps(quota, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(dump(os.environ)))
    except (ConfigError, EcoFlowError) as exc:
        sys.exit(str(exc))
