import contextlib
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
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

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)


class Decision(CallbackData, prefix="sub"):
    action: str
    chat_id: int


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
    outcome = await subscriptions.start(db, chat_id, user.full_name, user.username, settings.admin_chat_id)
    match outcome:
        case StartOutcome.SUBSCRIBED:
            await message.answer(texts.SUBSCRIBED, reply_markup=status_keyboard())
        case StartOutcome.ALREADY:
            await message.answer(texts.ALREADY_SUBSCRIBED, reply_markup=status_keyboard())
        case StartOutcome.PENDING:
            await message.answer(texts.REQUEST_PENDING)
        case StartOutcome.REJECTED:
            await message.answer(texts.REQUEST_REJECTED)
        case StartOutcome.REQUESTED:
            await message.answer(texts.REQUEST_SENT)
            await bot.send_message(
                settings.admin_chat_id,
                texts.admin_request_text(user.full_name, user.username, chat_id),
                reply_markup=approval_keyboard(chat_id),
            )


@router.callback_query(Decision.filter(F.action.in_({"approve", "reject"})))
async def on_decision(query: CallbackQuery, callback_data: Decision, db: Database, settings: Settings, bot: Bot) -> None:
    if query.from_user.id != settings.admin_chat_id:
        await query.answer()
        return
    approve = callback_data.action == "approve"
    subscriber = await subscriptions.decide(db, callback_data.chat_id, approve)
    if subscriber is None:
        await query.answer(texts.ALREADY_DECIDED)
        return
    with contextlib.suppress(TelegramForbiddenError):
        if approve:
            await bot.send_message(subscriber.chat_id, texts.SUBSCRIBED, reply_markup=status_keyboard())
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
async def on_status(message: Message, db: Database, settings: Settings, poller, schedules=None) -> None:
    if not await subscriptions.is_approved(db, message.chat.id):
        await message.answer(texts.NOT_SUBSCRIBED)
        return
    now = datetime.now(UTC)
    schedule = schedules.current(now) if schedules is not None else None
    battery = settings.thresholds.battery
    await message.answer(texts.status_text(poller.latest, poller.state, now, settings.tz, schedule, battery))


@router.message(Command("stop"))
async def on_stop(message: Message, db: Database) -> None:
    await subscriptions.stop(db, message.chat.id)
    await message.answer(texts.UNSUBSCRIBED, reply_markup=ReplyKeyboardRemove())


@router.message(Command("subscribers"))
async def on_subscribers(message: Message, db: Database, settings: Settings) -> None:
    if message.chat.id != settings.admin_chat_id:
        return
    subscribers = await db.subscribers(*_LIST_ACTIONS)
    if not subscribers:
        await message.answer(texts.NO_SUBSCRIBERS)
        return
    rows = [subscriber_buttons(s) for s in subscribers if s.chat_id != settings.admin_chat_id]
    await message.answer(
        texts.subscribers_text(subscribers),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@router.callback_query(Decision.filter(F.action == "remove"))
async def on_remove(query: CallbackQuery, callback_data: Decision, db: Database, settings: Settings) -> None:
    if query.from_user.id != settings.admin_chat_id:
        await query.answer()
        return
    subscriber = await subscriptions.remove(db, callback_data.chat_id)
    await query.answer(texts.removed_text(subscriber.name) if subscriber else texts.NOT_FOUND)


# Registered last on purpose: aiogram tries handlers in order, so this only sees what nothing else took.
# Service messages (a pinned alert) are not something the user said: no reply.
@router.message(~F.pinned_message)
async def on_other(message: Message, db: Database, settings: Settings) -> None:
    subscriber = await db.get_subscriber(message.chat.id)
    if subscriber is not None and subscriber.status is SubscriberStatus.APPROVED:
        admin = message.chat.id == settings.admin_chat_id
        await message.answer(texts.unsupported_text(admin), reply_markup=status_keyboard())
    elif subscriber is not None and subscriber.status is SubscriberStatus.PENDING:
        await message.answer(texts.REQUEST_PENDING)
    else:
        await message.answer(texts.NOT_SUBSCRIBED)
