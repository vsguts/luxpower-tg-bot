from datetime import datetime

from lxbot.device.snapshot import Snapshot
from lxbot.ecoflow.api import EcoFlowApi, EcoFlowConfig, EcoFlowError
from lxbot.ecoflow.quota import InvalidQuota, decode_quota

ONLINE = 1


class EcoFlowClient:
    def __init__(self, config: EcoFlowConfig):
        self._serial = config.serial
        self._api = EcoFlowApi(config)

    async def read_snapshot(self, taken_at: datetime) -> Snapshot:
        await self._ensure_online()
        try:
            return decode_quota(await self._api.quota(), taken_at)
        except InvalidQuota as exc:
            raise EcoFlowError(str(exc)) from exc

    async def close(self) -> None:
        await self._api.close()

    # The cloud answers an offline station's quota with its last values: a blackout would never show.
    async def _ensure_online(self) -> None:
        devices = await self._api.devices()
        station = next((d for d in devices if d.get("sn") == self._serial), None)
        if station is None:
            raise EcoFlowError(f"station {self._serial} is not bound to this EcoFlow account")
        if station.get("online") != ONLINE:
            raise EcoFlowError(f"station {self._serial} is offline")
