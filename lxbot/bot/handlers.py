import contextlib
import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from lxbot.bot import subscriptions, texts
from lxbot.bot.subscriptions import StartOutcome
from lxbot.config import Settings
from lxbot.storage.db import Database, Subscriber, SubscriberStatus

log = logging.getLogger(__name__)
router = Router()
_USER_CONTENT = frozenset(
    {
        ContentType.TEXT,
        ContentType.STICKER,
        ContentType.PHOTO,
        ContentType.VIDEO,
        ContentType.VIDEO_NOTE,
        ContentType.ANIMATION,
        ContentType.VOICE,
        ContentType.AUDIO,
        ContentType.DOCUMENT,
        ContentType.CONTACT,
        ContentType.LOCATION,
        ContentType.POLL,
    }
)
router.message.filter(F.chat.type == ChatType.PRIVATE)


class Decision(CallbackData, prefix="sub"):
    action: str
    chat_id: int


class UpsTest(CallbackData, prefix="ups"):
    confirm: bool


def status_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=texts.STATUS_BUTTON)]], resize_keyboard=True)


def approval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Схвалити", callback_data=Decision(action="approve", chat_id=chat_id).pack()),
                InlineKeyboardButton(text="❌ Відхилити", callback_data=Decision(action="reject", chat_id=chat_id).pack()),
            ]
        ]
    )


_LIST_ACTIONS = {
    SubscriberStatus.APPROVED: [("❌", "remove")],
    SubscriberStatus.PENDING: [("✅", "approve"), ("❌", "reject")],
    SubscriberStatus.REJECTED: [("↩️", "approve")],
}


def subscriber_buttons(subscriber: Subscriber) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=f"{icon} {subscriber.name}",
            callback_data=Decision(action=action, chat_id=subscriber.chat_id).pack(),
        )
        for icon, action in _LIST_ACTIONS[subscriber.status]
    ]


@router.message(CommandStart())
async def on_start(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    user = message.from_user
    chat_id = message.chat.id
    outcome = await subscriptions.start(db, chat_id, user.full_name, user.username, settings.admin_chat_ids)
    match outcome:
        case StartOutcome.SUBSCRIBED:
            await message.answer(texts.subscribed_text(settings.has_generator), reply_markup=status_keyboard())
        case StartOutcome.ALREADY:
            await message.answer(texts.ALREADY_SUBSCRIBED, reply_markup=status_keyboard())
        case StartOutcome.PENDING:
            await message.answer(texts.REQUEST_PENDING)
        case StartOutcome.REJECTED:
            await message.answer(texts.REQUEST_REJECTED)
        case StartOutcome.REQUESTED:
            await message.answer(texts.REQUEST_SENT)
            request = texts.admin_request_text(user.full_name, user.username, chat_id)
            for admin in settings.admin_chat_ids:
                # An admin who never opened the bot cannot be written to; the others still get the request.
                try:
                    await bot.send_message(admin, request, reply_markup=approval_keyboard(chat_id))
                except (TelegramForbiddenError, TelegramBadRequest) as exc:
                    log.warning("cannot send the request to admin %s: %s", admin, exc)


@router.callback_query(Decision.filter(F.action.in_({"approve", "reject"})))
async def on_decision(query: CallbackQuery, callback_data: Decision, db: Database, settings: Settings, bot: Bot) -> None:
    if not settings.is_admin(query.from_user.id):
        await query.answer()
        return
    approve = callback_data.action == "approve"
    subscriber = await subscriptions.decide(db, callback_data.chat_id, approve)
    if subscriber is None:
        await query.answer(texts.ALREADY_DECIDED)
        return
    with contextlib.suppress(TelegramForbiddenError):
        if approve:
            text = texts.subscribed_text(settings.has_generator)
            await bot.send_message(subscriber.chat_id, text, reply_markup=status_keyboard())
        else:
            await bot.send_message(subscriber.chat_id, texts.REQUEST_REJECTED)
    # Telegram hands over an inaccessible message for requests older than ~48 h; the decision is already stored.
    if isinstance(query.message, Message):
        mark = texts.decision_mark(approve, subscriber.name)
        with contextlib.suppress(TelegramBadRequest):
            await query.message.edit_text(f"{query.message.html_text}\n\n{mark}")
    await query.answer()


@router.message(Command("status"))
@router.message(F.text == texts.STATUS_BUTTON)
async def on_status(message: Message, db: Database, settings: Settings, poller, schedules=None, ups=None) -> None:
    if not await subscriptions.is_approved(db, message.chat.id):
        await message.answer(texts.NOT_SUBSCRIBED)
        return
    now = datetime.now(UTC)
    schedule = schedules.current(now) if schedules is not None else None
    battery = settings.thresholds.battery
    text = texts.status_text(
        poller.latest, poller.state, now, settings.tz, schedule, battery, generator=settings.has_generator
    )
    if ups is not None and ups.test_low and settings.is_admin(message.chat.id):
        text += f"\n{texts.UPS_TEST_STATUS}"
    await message.answer(text)


@router.message(Command("stop"))
async def on_stop(message: Message, db: Database) -> None:
    await subscriptions.stop(db, message.chat.id)
    await message.answer(texts.UNSUBSCRIBED, reply_markup=ReplyKeyboardRemove())


@router.message(Command("subscribers"))
async def on_subscribers(message: Message, db: Database, settings: Settings) -> None:
    if not settings.is_admin(message.chat.id):
        return
    subscribers = await db.subscribers(*_LIST_ACTIONS)
    if not subscribers:
        await message.answer(texts.NO_SUBSCRIBERS)
        return
    rows = [subscriber_buttons(s) for s in subscribers if not settings.is_admin(s.chat_id)]
    await message.answer(
        texts.subscribers_text(subscribers),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@router.callback_query(Decision.filter(F.action == "remove"))
async def on_remove(query: CallbackQuery, callback_data: Decision, db: Database, settings: Settings) -> None:
    if not settings.is_admin(query.from_user.id):
        await query.answer()
        return
    subscriber = await subscriptions.remove(db, callback_data.chat_id)
    await query.answer(texts.removed_text(subscriber.name) if subscriber else texts.NOT_FOUND)


@router.message(Command("ups_test"))
async def on_ups_test(message: Message, settings: Settings, ups=None) -> None:
    if not settings.is_admin(message.chat.id):
        return
    if ups is None:
        await message.answer(texts.UPS_NOT_CONFIGURED)
        return
    buttons = [
        InlineKeyboardButton(text="🔻 Так, тестувати", callback_data=UpsTest(confirm=True).pack()),
        InlineKeyboardButton(text="Скасувати", callback_data=UpsTest(confirm=False).pack()),
    ]
    await message.answer(texts.UPS_TEST_CONFIRM, reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]))


@router.callback_query(UpsTest.filter())
async def on_ups_test_answer(query: CallbackQuery, callback_data: UpsTest, settings: Settings, ups=None) -> None:
    if not settings.is_admin(query.from_user.id) or ups is None:
        await query.answer()
        return
    if callback_data.confirm:
        ups.test_low = True
        log.warning("UPS test: NUT clients now see a low battery")
    text = texts.UPS_TEST_ON if callback_data.confirm else texts.UPS_TEST_CANCELLED
    if isinstance(query.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await query.message.edit_text(text)
    await query.answer()


@router.message(Command("ups_reset"))
async def on_ups_reset(message: Message, settings: Settings, ups=None) -> None:
    if not settings.is_admin(message.chat.id):
        return
    if ups is None:
        await message.answer(texts.UPS_NOT_CONFIGURED)
        return
    ups.test_low = False
    log.warning("UPS test ended: NUT clients see the inverter again")
    await message.answer(texts.UPS_TEST_OFF)


# Registered last on purpose: aiogram tries handlers in order, so this only sees what nothing else took.
# Service messages (a pinned alert, an auto-delete timer) are not something the user said: no reply.
@router.message(F.content_type.in_(_USER_CONTENT))
async def on_other(message: Message, db: Database, settings: Settings) -> None:
    subscriber = await db.get_subscriber(message.chat.id)
    if subscriber is not None and subscriber.status is SubscriberStatus.APPROVED:
        admin = settings.is_admin(message.chat.id)
        await message.answer(texts.unsupported_text(admin, settings.ups is not None), reply_markup=status_keyboard())
    elif subscriber is not None and subscriber.status is SubscriberStatus.PENDING:
        await message.answer(texts.REQUEST_PENDING)
    else:
        await message.answer(texts.NOT_SUBSCRIBED)
