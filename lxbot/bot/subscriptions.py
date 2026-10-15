from enum import StrEnum

from lxbot.storage.db import Database, Subscriber, SubscriberStatus


class StartOutcome(StrEnum):
    SUBSCRIBED = "subscribed"
    ALREADY = "already"
    PENDING = "pending"
    REQUESTED = "requested"
    REJECTED = "rejected"


async def start(db: Database, chat_id: int, name: str, username: str | None, admin_chat_id: int) -> StartOutcome:
    existing = await db.get_subscriber(chat_id)
    status = existing.status if existing else None
    if status is SubscriberStatus.APPROVED:
        return StartOutcome.ALREADY
    if status is SubscriberStatus.PENDING:
        return StartOutcome.PENDING
    if status is SubscriberStatus.REJECTED:
        return StartOutcome.REJECTED
    # A user who left with /stop was approved before, so coming back needs no new approval.
    if chat_id == admin_chat_id or status is SubscriberStatus.INACTIVE:
        await db.save_subscriber(chat_id, name, username, SubscriberStatus.APPROVED)
        return StartOutcome.SUBSCRIBED
    await db.save_subscriber(chat_id, name, username, SubscriberStatus.PENDING)
    return StartOutcome.REQUESTED


# A rejected user can still be approved, so a mistaken ❌ is reversible from /subscribers.
_DECIDABLE = {
    True: {SubscriberStatus.PENDING, SubscriberStatus.REJECTED},
    False: {SubscriberStatus.PENDING},
}


async def decide(db: Database, chat_id: int, approve: bool) -> Subscriber | None:
    subscriber = await db.get_subscriber(chat_id)
    if subscriber is None or subscriber.status not in _DECIDABLE[approve]:
        return None
    status = SubscriberStatus.APPROVED if approve else SubscriberStatus.REJECTED
    await db.set_subscriber_status(chat_id, status)
    return await db.get_subscriber(chat_id)


async def stop(db: Database, chat_id: int) -> None:
    if await is_approved(db, chat_id):
        await db.set_subscriber_status(chat_id, SubscriberStatus.INACTIVE)


async def remove(db: Database, chat_id: int) -> Subscriber | None:
    if await db.get_subscriber(chat_id) is None:
        return None
    await db.set_subscriber_status(chat_id, SubscriberStatus.REJECTED)
    return await db.get_subscriber(chat_id)


async def is_approved(db: Database, chat_id: int) -> bool:
    subscriber = await db.get_subscriber(chat_id)
    return subscriber is not None and subscriber.status is SubscriberStatus.APPROVED
