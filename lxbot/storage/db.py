import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SubscriberStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class Subscriber:
    chat_id: int
    name: str
    username: str | None
    status: SubscriberStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoredEvent:
    at: datetime
    type: str
    payload: dict


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _subscriber(row: aiosqlite.Row) -> Subscriber:
    return Subscriber(
        chat_id=row["chat_id"],
        name=row["name"],
        username=row["username"],
        status=SubscriberStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> "Database":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def get_subscriber(self, chat_id: int) -> Subscriber | None:
        async with self._conn.execute("SELECT * FROM subscribers WHERE chat_id = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
        return _subscriber(row) if row else None

    async def save_subscriber(
        self, chat_id: int, name: str, username: str | None, status: SubscriberStatus
    ) -> Subscriber:
        now = _now()
        await self._conn.execute(
            """
            INSERT INTO subscribers (chat_id, name, username, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (chat_id, name, username, status.value, now, now),
        )
        await self._conn.commit()
        return await self.get_subscriber(chat_id)

    async def set_subscriber_status(self, chat_id: int, status: SubscriberStatus) -> None:
        await self._conn.execute(
            "UPDATE subscribers SET status = ?, updated_at = ? WHERE chat_id = ?",
            (status.value, _now(), chat_id),
        )
        await self._conn.commit()

    async def subscribers(self, *statuses: SubscriberStatus) -> list[Subscriber]:
        query = "SELECT * FROM subscribers"
        params: tuple = ()
        if statuses:
            query += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            params = tuple(s.value for s in statuses)
        async with self._conn.execute(query + " ORDER BY created_at, chat_id", params) as cursor:
            return [_subscriber(row) for row in await cursor.fetchall()]

    async def add_event(self, event_type: str, at: datetime, payload: dict) -> None:
        await self._conn.execute(
            "INSERT INTO events (at, type, payload) VALUES (?, ?, ?)",
            (at.isoformat(), str(event_type), json.dumps(payload)),
        )
        await self._conn.commit()

    async def recent_events(self, limit: int = 100) -> list[StoredEvent]:
        async with self._conn.execute(
            "SELECT at, type, payload FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [StoredEvent(datetime.fromisoformat(r["at"]), r["type"], json.loads(r["payload"])) for r in rows]

    async def load_state(self, key: str) -> dict | None:
        async with self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["value"]) if row else None

    async def save_state(self, key: str, value: dict) -> None:
        await self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        await self._conn.commit()
