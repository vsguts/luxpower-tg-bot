from lxbot.storage.db import Database, SubscriberStatus
from tests.factories import at


async def test_save_and_get_subscriber(db):
    saved = await db.save_subscriber(42, "Оля", "olya", SubscriberStatus.PENDING)
    assert saved.chat_id == 42
    assert saved.status is SubscriberStatus.PENDING
    assert await db.get_subscriber(42) == saved
    assert await db.get_subscriber(7) is None


async def test_save_subscriber_updates_and_keeps_created_at(db):
    first = await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    second = await db.save_subscriber(42, "Ольга", "olga", SubscriberStatus.APPROVED)
    assert second.created_at == first.created_at
    assert second.name == "Ольга"
    assert second.status is SubscriberStatus.APPROVED


async def test_filter_subscribers_by_status(db):
    await db.save_subscriber(1, "A", None, SubscriberStatus.APPROVED)
    await db.save_subscriber(2, "B", None, SubscriberStatus.PENDING)
    await db.save_subscriber(3, "C", None, SubscriberStatus.REJECTED)
    await db.set_subscriber_status(2, SubscriberStatus.APPROVED)
    approved = await db.subscribers(SubscriberStatus.APPROVED)
    assert [s.chat_id for s in approved] == [1, 2]
    assert len(await db.subscribers()) == 3


async def test_events_journal(db):
    await db.add_event("grid_lost", at(1), {"soc": 80})
    await db.add_event("grid_restored", at(5), {"soc": 70})
    events = await db.recent_events()
    assert [e.type for e in events] == ["grid_restored", "grid_lost"]
    assert events[1].at == at(1)
    assert events[1].payload == {"soc": 80}


async def test_state_round_trip(db):
    assert await db.load_state("detector") is None
    await db.save_state("detector", {"a": 1})
    await db.save_state("detector", {"a": 2})
    assert await db.load_state("detector") == {"a": 2}


async def test_data_survives_reopen(tmp_path):
    path = tmp_path / "bot.sqlite3"
    first = await Database.open(path)
    await first.save_state("detector", {"a": 1})
    await first.close()
    second = await Database.open(path)
    assert await second.load_state("detector") == {"a": 1}
    await second.close()
