from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lxbot.config import Settings
from lxbot.luxpower.client import DongleConfig
from lxbot.monitor.detector import Thresholds
from lxbot.storage.db import Database
from tests.fixtures import DONGLE_SERIAL, INVERTER_SERIAL


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(tmp_path / "data" / "bot.sqlite3")
    yield database
    await database.close()


def make_settings(admin_chat_id: int = 1) -> Settings:
    return Settings(
        bot_token="test-token",
        admin_chat_id=admin_chat_id,
        device=DongleConfig("127.0.0.1", 8000, DONGLE_SERIAL, INVERTER_SERIAL),
        thresholds=Thresholds(),
        poll_interval=15,
        tz=ZoneInfo("Europe/Kyiv"),
        db_path=Path("unused.sqlite3"),
    )
