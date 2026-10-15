import contextlib
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError
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
from lxbot.storage.db import Database, SubscriberStatus

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
    mark = texts.APPROVED_MARK if approve else texts.REJECTED_MARK
    await query.message.edit_text(f"{query.message.html_text}\n\n{mark}")
    with contextlib.suppress(TelegramForbiddenError):
        if approve:
            await bot.send_message(subscriber.chat_id, texts.SUBSCRIBED, reply_markup=status_keyboard())
        else:
            await bot.send_message(subscriber.chat_id, texts.REQUEST_REJECTED)
    await query.answer()


@router.message(Command("status"))
@router.message(F.text == texts.STATUS_BUTTON)
async def on_status(message: Message, db: Database, settings: Settings, poller) -> None:
    if not await subscriptions.is_approved(db, message.chat.id):
        await message.answer(texts.NOT_SUBSCRIBED)
        return
    await message.answer(texts.status_text(poller.latest, poller.state, datetime.now(UTC), settings.tz))


@router.message(Command("stop"))
async def on_stop(message: Message, db: Database) -> None:
    await subscriptions.stop(db, message.chat.id)
    await message.answer(texts.UNSUBSCRIBED, reply_markup=ReplyKeyboardRemove())


@router.message(Command("subscribers"))
async def on_subscribers(message: Message, db: Database, settings: Settings) -> None:
    if message.chat.id != settings.admin_chat_id:
        return
    subscribers = await db.subscribers(SubscriberStatus.APPROVED, SubscriberStatus.PENDING)
    if not subscribers:
        await message.answer(texts.NO_SUBSCRIBERS)
        return
    rows = [
        [InlineKeyboardButton(text=f"❌ {s.name}", callback_data=Decision(action="remove", chat_id=s.chat_id).pack())]
        for s in subscribers
        if s.chat_id != settings.admin_chat_id
    ]
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
