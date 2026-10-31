from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ContentType
from aiogram.types import Message

from lxbot.bot import texts
from lxbot.bot.handlers import (
    Decision,
    UpsTest,
    on_decision,
    on_other,
    on_remove,
    on_start,
    on_status,
    on_stop,
    on_subscribers,
    on_ups_reset,
    on_ups_test,
    on_ups_test_answer,
    router,
)
from lxbot.monitor.detector import Debounced, State, initial_state
from lxbot.monitor.forecast import BatteryConfig
from lxbot.schedule.model import DaySchedule, Outage, Schedule
from lxbot.storage.db import SubscriberStatus
from tests.conftest import make_settings
from tests.factories import at, make_snapshot
from tests.schedule_factories import GROUP, KYIV

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
    admin_message = MagicMock(spec=Message, html_text="🆕 Новий запит", edit_text=AsyncMock())
    query = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN), message=admin_message, answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    await on_decision(query, Decision(action="approve", chat_id=42), db=db, settings=make_settings(ADMIN), bot=bot)
    assert (await db.get_subscriber(42)).status is SubscriberStatus.APPROVED
    assert bot.send_message.await_args.args == (42, texts.SUBSCRIBED)
    assert texts.APPROVED_MARK in admin_message.edit_text.await_args.args[0]


async def test_approval_survives_an_inaccessible_admin_message(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    query = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN), message=None, answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    await on_decision(query, Decision(action="approve", chat_id=42), db=db, settings=make_settings(ADMIN), bot=bot)
    assert (await db.get_subscriber(42)).status is SubscriberStatus.APPROVED
    assert bot.send_message.await_args.args == (42, texts.SUBSCRIBED)
    query.answer.assert_awaited_once_with()


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


async def test_subscribers_offers_a_fix_for_every_status(db):
    await db.save_subscriber(ADMIN, "Admin", None, SubscriberStatus.APPROVED)
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.APPROVED)
    await db.save_subscriber(43, "Петро", None, SubscriberStatus.PENDING)
    await db.save_subscriber(44, "Ігор", None, SubscriberStatus.REJECTED)
    await db.save_subscriber(45, "Пішов", None, SubscriberStatus.INACTIVE)
    msg = message(ADMIN)
    await on_subscribers(msg, db=db, settings=make_settings(ADMIN))
    text = msg.answer.await_args.args[0]
    assert "🚫 Ігор" in text
    assert "Пішов" not in text
    keyboard = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert [[(b.text, b.callback_data) for b in row] for row in keyboard] == [
        [("❌ Оля", Decision(action="remove", chat_id=42).pack())],
        [
            ("✅ Петро", Decision(action="approve", chat_id=43).pack()),
            ("❌ Петро", Decision(action="reject", chat_id=43).pack()),
        ],
        [("↩️ Ігор", Decision(action="approve", chat_id=44).pack())],
    ]


async def test_subscribers_is_admin_only(db):
    msg = message(42)
    await on_subscribers(msg, db=db, settings=make_settings(ADMIN))
    msg.answer.assert_not_awaited()


async def test_a_rejected_user_can_be_approved_from_the_list(db):
    await db.save_subscriber(44, "Ігор", None, SubscriberStatus.REJECTED)
    admin_message = MagicMock(spec=Message, html_text="👥 Підписники:", edit_text=AsyncMock())
    query = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN), message=admin_message, answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    await on_decision(query, Decision(action="approve", chat_id=44), db=db, settings=make_settings(ADMIN), bot=bot)
    assert (await db.get_subscriber(44)).status is SubscriberStatus.APPROVED
    assert bot.send_message.await_args.args == (44, texts.SUBSCRIBED)
    assert "Ігор" in admin_message.edit_text.await_args.args[0]


def test_router_registers_every_handler():
    message_callbacks = [h.callback for h in router.message.handlers]
    query_callbacks = [h.callback for h in router.callback_query.handlers]
    assert message_callbacks.count(on_status) == 2
    for callback in (on_start, on_stop, on_subscribers):
        assert callback in message_callbacks
    # The catch-all must come last, or it would answer every command.
    assert message_callbacks[-1] is on_other
    for callback in (on_decision, on_remove, on_ups_test_answer):
        assert callback in query_callbacks
    for callback in (on_ups_test, on_ups_reset):
        assert callback in message_callbacks


async def test_status_shows_the_next_planned_outage(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.APPROVED)
    snapshot = make_snapshot(0, soc=80)
    poller = SimpleNamespace(latest=snapshot, state=initial_state(snapshot))
    # on_status uses the real clock, so the outage is placed relative to it.
    start = datetime.now(UTC) + timedelta(hours=2)
    day = start.astimezone(KYIV).date()
    schedule = Schedule(GROUP, {day: DaySchedule(GROUP, day, (Outage(start, start + timedelta(hours=4)),))})
    schedules = SimpleNamespace(current=lambda now: schedule)
    msg = message(42)
    await on_status(msg, db=db, settings=make_settings(ADMIN), poller=poller, schedules=schedules)
    assert "📅 Наступне відключення:" in msg.answer.await_args.args[0]


async def test_free_text_from_a_subscriber_gets_the_command_hint(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.APPROVED)
    msg = message(42)
    await on_other(msg, db=db, settings=make_settings(ADMIN))
    assert msg.answer.await_args.args == (texts.unsupported_text(admin=False),)
    assert msg.answer.await_args.kwargs["reply_markup"] is not None


async def test_free_text_from_the_admin_lists_subscribers_too(db):
    await db.save_subscriber(ADMIN, "Адмін", None, SubscriberStatus.APPROVED)
    msg = message(ADMIN)
    await on_other(msg, db=db, settings=make_settings(ADMIN))
    assert msg.answer.await_args.args == (texts.unsupported_text(admin=True),)


async def test_free_text_from_a_pending_user_says_the_request_is_pending(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    msg = message(42)
    await on_other(msg, db=db, settings=make_settings(ADMIN))
    msg.answer.assert_awaited_once_with(texts.REQUEST_PENDING)


async def test_free_text_from_a_stranger_points_to_start(db):
    msg = message(42)
    await on_other(msg, db=db, settings=make_settings(ADMIN))
    msg.answer.assert_awaited_once_with(texts.NOT_SUBSCRIBED)


async def test_status_shows_the_battery_forecast(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.APPROVED)
    snapshot = make_snapshot(480, grid=False, soc=60, battery_w=-1100)
    state = State(grid=Debounced(False, at(0)), gen=Debounced(False, at(0)), drain_soc=72, drain_since=at(0))
    poller = SimpleNamespace(latest=snapshot, state=state)
    base = make_settings(ADMIN)
    settings = replace(base, thresholds=replace(base.thresholds, battery=BatteryConfig(16000, 5)))
    msg = message(42)
    await on_status(msg, db=db, settings=settings, poller=poller)
    assert "⏳ Вистачить: ~8 год при 1.1 кВт" in msg.answer.await_args.args[0]


@pytest.mark.parametrize(
    ("content_type", "answered"),
    [
        (ContentType.TEXT, True),
        (ContentType.STICKER, True),
        (ContentType.PHOTO, True),
        (ContentType.VOICE, True),
        (ContentType.PINNED_MESSAGE, False),
        (ContentType.MESSAGE_AUTO_DELETE_TIMER_CHANGED, False),
        (ContentType.WRITE_ACCESS_ALLOWED, False),
    ],
)
def test_the_catch_all_answers_only_what_a_person_sent(content_type, answered):
    (catch_all,) = [h for h in router.message.handlers if h.callback is on_other]
    update = SimpleNamespace(content_type=content_type)
    assert all(f.callback(update) for f in catch_all.filters) is answered


def ups_query(chat_id, confirm):
    admin_message = MagicMock(spec=Message, edit_text=AsyncMock())
    query = SimpleNamespace(from_user=SimpleNamespace(id=chat_id), message=admin_message, answer=AsyncMock())
    return query, UpsTest(confirm=confirm)


async def test_ups_test_asks_the_admin_to_confirm():
    ups, msg = SimpleNamespace(test_low=False), message(ADMIN)
    await on_ups_test(msg, settings=make_settings(ADMIN), ups=ups)
    assert msg.answer.await_args.args == (texts.UPS_TEST_CONFIRM,)
    assert msg.answer.await_args.kwargs["reply_markup"] is not None
    assert ups.test_low is False


async def test_a_confirmed_ups_test_reports_a_low_battery_until_reset():
    ups = SimpleNamespace(test_low=False)
    query, data = ups_query(ADMIN, confirm=True)
    await on_ups_test_answer(query, data, settings=make_settings(ADMIN), ups=ups)
    assert ups.test_low is True
    assert query.message.edit_text.await_args.args == (texts.UPS_TEST_ON,)
    msg = message(ADMIN)
    await on_ups_reset(msg, settings=make_settings(ADMIN), ups=ups)
    assert ups.test_low is False
    assert msg.answer.await_args.args == (texts.UPS_TEST_OFF,)


async def test_a_cancelled_ups_test_changes_nothing():
    ups = SimpleNamespace(test_low=False)
    query, data = ups_query(ADMIN, confirm=False)
    await on_ups_test_answer(query, data, settings=make_settings(ADMIN), ups=ups)
    assert ups.test_low is False
    assert query.message.edit_text.await_args.args == (texts.UPS_TEST_CANCELLED,)


async def test_ups_commands_are_admin_only():
    ups = SimpleNamespace(test_low=True)
    msg = message(42)
    await on_ups_test(msg, settings=make_settings(ADMIN), ups=ups)
    await on_ups_reset(msg, settings=make_settings(ADMIN), ups=ups)
    msg.answer.assert_not_awaited()
    query, data = ups_query(42, confirm=False)
    await on_ups_test_answer(query, data, settings=make_settings(ADMIN), ups=ups)
    assert ups.test_low is True
    query.message.edit_text.assert_not_awaited()


async def test_ups_commands_without_a_nut_setup():
    msg = message(ADMIN)
    await on_ups_test(msg, settings=make_settings(ADMIN), ups=None)
    assert msg.answer.await_args.args == (texts.UPS_NOT_CONFIGURED,)


async def test_the_admin_status_shows_a_running_ups_test(db):
    for chat_id in (ADMIN, 42):
        await db.save_subscriber(chat_id, "x", None, SubscriberStatus.APPROVED)
    snapshot = make_snapshot(0, soc=80)
    poller = SimpleNamespace(latest=snapshot, state=initial_state(snapshot))
    ups = SimpleNamespace(test_low=True)
    admin, other = message(ADMIN), message(42)
    await on_status(admin, db=db, settings=make_settings(ADMIN), poller=poller, ups=ups)
    await on_status(other, db=db, settings=make_settings(ADMIN), poller=poller, ups=ups)
    assert texts.UPS_TEST_STATUS in admin.answer.await_args.args[0]
    assert texts.UPS_TEST_STATUS not in other.answer.await_args.args[0]


async def test_approval_without_a_generator_does_not_promise_generator_alerts(db):
    await db.save_subscriber(42, "Оля", None, SubscriberStatus.PENDING)
    query = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN), message=None, answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    settings = replace(make_settings(ADMIN), has_generator=False)
    await on_decision(query, Decision(action="approve", chat_id=42), db=db, settings=settings, bot=bot)
    assert bot.send_message.await_args.args == (42, texts.SUBSCRIBED_NO_GENERATOR)
