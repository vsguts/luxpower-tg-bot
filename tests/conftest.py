import pytest

from lxbot.storage.db import Database


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(tmp_path / "data" / "bot.sqlite3")
    yield database
    await database.close()
