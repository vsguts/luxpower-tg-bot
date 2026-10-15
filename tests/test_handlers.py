from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Dispatcher

from lxbot.bot import texts
from lxbot.bot.handlers import Decision, on_decision, on_remove, on_start, on_status, on_stop, on_subscribers, router
from lxbot.monitor.detector import initial_state
from lxbot.storage.db import SubscriberStatus
from tests.conftest import make_settings
from tests.factories import make_snapshot

ADMIN = 1


def message(chat_id: int, name: str = "Оля <3", username: str | None = "olya"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=chat_id, full_name=name, username=username),
        answer=AsyncMock(),
    )


async def test_start_sends_an_escaped_request_to_the_admin(db):
    msg, bot = message(42), SimpleNamespace(send_message=AsyncMock())
    await on_start(msg, db=db, settings=make_settings(ADMIN), bot=bot)
    msg.answer.assert_awaited_once_with(texts.REQUEST_SENT)
    chat_id, text = bot.send_message.await_args.args
    assert chat_id == ADMIN
    assert "Оля &lt;3" in text
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None


async def test_admin_approval_notifies_the_user(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN),
        message=SimpleNamespace(html_text="🆕 Новий запит", edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    await on_decision(query, Decision(action="approve", chat_id=42), db=db, settings=make_settings(ADMIN), bot=bot)
    assert (await db.get_subscriber(42)).status is SubscriberStatus.APPROVED
    assert bot.send_message.await_args.args == (42, texts.SUBSCRIBED)
    assert texts.APPROVED_MARK in query.message.edit_text.await_args.args[0]


async def test_decision_from_a_non_admin_is_ignored(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    query = SimpleNamespace(from_user=SimpleNamespace(id=99), message=None, answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    await on_decision(query, Decision(action="approve", chat_id=42), db=db, settings=make_settings(ADMIN), bot=bot)
    assert (await db.get_subscriber(42)).status is SubscriberStatus.PENDING
    bot.send_message.assert_not_awaited()


async def test_status_requires_approval(db):
    msg = message(42)
    poller = SimpleNamespace(latest=None, state=None)
    await on_status(msg, db=db, settings=make_settings(ADMIN), poller=poller)
    msg.answer.assert_awaited_once_with(texts.NOT_SUBSCRIBED)


async def test_status_for_an_approved_user(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.APPROVED)
    snapshot = make_snapshot(0, soc=80)
    poller = SimpleNamespace(latest=snapshot, state=initial_state(snapshot))
    msg = message(42)
    await on_status(msg, db=db, settings=make_settings(ADMIN), poller=poller)
    assert "🔋 Батарея: 80%" in msg.answer.await_args.args[0]


async def test_router_registers_every_handler(db):
    dispatcher = Dispatcher(db=db, settings=make_settings(ADMIN), poller=SimpleNamespace(latest=None, state=None))
    dispatcher.include_router(router)
    message_callbacks = [h.callback for h in router.message.handlers]
    query_callbacks = [h.callback for h in router.callback_query.handlers]
    assert message_callbacks.count(on_status) == 2
    for callback in (on_start, on_stop, on_subscribers):
        assert callback in message_callbacks
    for callback in (on_decision, on_remove):
        assert callback in query_callbacks
