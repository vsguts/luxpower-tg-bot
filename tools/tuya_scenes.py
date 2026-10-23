"""Find the IDs of Tuya tap-to-run scenes and try one, with the TUYA_* keys from .env.

    docker compose run --rm -T bot python -m tools.tuya_scenes --device <device id>   # its home
    docker compose run --rm -T bot python -m tools.tuya_scenes --home <home id>       # its scenes
    docker compose run --rm -T bot python -m tools.tuya_scenes --run <scene id>       # runs it

Device IDs are on the platform's project page (Devices tab); the home of any device will do.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping

from lxbot.tuya.api import DEFAULT_API_URL, TuyaApi, TuyaConfig, TuyaError


def _config(env: Mapping[str, str]) -> TuyaConfig:
    access_id, secret = env.get("TUYA_ACCESS_ID", "").strip(), env.get("TUYA_ACCESS_SECRET", "").strip()
    if not access_id or not secret:
        sys.exit("TUYA_ACCESS_ID and TUYA_ACCESS_SECRET must be set in .env")
    return TuyaConfig(access_id, secret, api_url=(env.get("TUYA_API_URL") or DEFAULT_API_URL).rstrip("/"))


async def _device(api: TuyaApi, device_id: str) -> None:
    device = await api.request("GET", f"/v1.0/devices/{device_id}")
    if not isinstance(device, dict):
        raise TuyaError(f"unexpected device reply: {device!r}")
    print(f"{device.get('name')}: home {device.get('owner_id')}, online {device.get('online')}")


async def _scenes(api: TuyaApi, home_id: str) -> None:
    try:
        reply = await api.request("GET", "/v2.0/cloud/scene/rule", {"space_id": home_id, "type": "scene"})
        rules = reply.get("list", []) if isinstance(reply, dict) else []
        print("v2 scene rules:")
        for rule in rules:
            print(f"  {rule.get('id')}  {rule.get('name')}  ({rule.get('status')}, {rule.get('running_mode')})")
        return
    except TuyaError as exc:
        print(f"v2 scene rules failed: {exc}")
    scenes = await api.request("GET", f"/v1.1/homes/{home_id}/scenes")
    print("v1 scenes:")
    for scene in scenes if isinstance(scenes, list) else []:
        print(f"  {scene.get('scene_id')}  {scene.get('name')}  (enabled {scene.get('enabled')})")


async def main(env: Mapping[str, str], args: argparse.Namespace) -> None:
    api = TuyaApi(_config(env))
    try:
        if args.device:
            await _device(api, args.device)
        if args.home:
            await _scenes(api, args.home)
        if args.run:
            await api.trigger_scene(args.run)
            print(f"scene {args.run} triggered")
    finally:
        await api.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", help="print the home a device belongs to")
    parser.add_argument("--home", help="list the tap-to-run scenes of a home")
    parser.add_argument("--run", help="trigger a tap-to-run scene")
    arguments = parser.parse_args()
    if not (arguments.device or arguments.home or arguments.run):
        parser.error("pass --device, --home or --run")
    try:
        asyncio.run(main(os.environ, arguments))
    except TuyaError as exc:
        sys.exit(str(exc))
