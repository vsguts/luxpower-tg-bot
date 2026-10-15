from lxbot.bot import subscriptions
from lxbot.bot.subscriptions import StartOutcome
from lxbot.storage.db import SubscriberStatus

ADMIN = 1


async def start(db, chat_id):
    return await subscriptions.start(db, chat_id, "Оля", "olya", ADMIN)


async def test_new_user_requests_access(db):
    assert await start(db, 42) is StartOutcome.REQUESTED
    assert (await db.get_subscriber(42)).status is SubscriberStatus.PENDING
    assert await start(db, 42) is StartOutcome.PENDING


async def test_admin_is_subscribed_at_once(db):
    assert await start(db, ADMIN) is StartOutcome.SUBSCRIBED
    assert await subscriptions.is_approved(db, ADMIN)


async def test_approve_and_already_subscribed(db):
    await start(db, 42)
    approved = await subscriptions.decide(db, 42, approve=True)
    assert approved.status is SubscriberStatus.APPROVED
    assert await start(db, 42) is StartOutcome.ALREADY


async def test_decide_twice_is_noop(db):
    await start(db, 42)
    assert await subscriptions.decide(db, 42, approve=False) is not None
    assert await subscriptions.decide(db, 42, approve=True) is None
    assert (await db.get_subscriber(42)).status is SubscriberStatus.REJECTED


async def test_rejected_user_cannot_request_again(db):
    await start(db, 42)
    await subscriptions.decide(db, 42, approve=False)
    assert await start(db, 42) is StartOutcome.REJECTED


async def test_stop_then_start_resubscribes_without_approval(db):
    await start(db, 42)
    await subscriptions.decide(db, 42, approve=True)
    await subscriptions.stop(db, 42)
    assert not await subscriptions.is_approved(db, 42)
    assert await start(db, 42) is StartOutcome.SUBSCRIBED


async def test_removed_user_needs_nothing_and_cannot_return(db):
    await start(db, 42)
    await subscriptions.decide(db, 42, approve=True)
    removed = await subscriptions.remove(db, 42)
    assert removed.status is SubscriberStatus.REJECTED
    assert await start(db, 42) is StartOutcome.REJECTED
    assert await subscriptions.remove(db, 7) is None
