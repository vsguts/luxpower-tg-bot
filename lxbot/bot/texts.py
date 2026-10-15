from datetime import datetime, timedelta, tzinfo
from html import escape

from lxbot.luxpower.registers import Snapshot
from lxbot.monitor.detector import Event, EventType, State
from lxbot.storage.db import Subscriber, SubscriberStatus

STALE_AFTER = timedelta(minutes=2)

STATUS_BUTTON = "📊 Статус"
REQUEST_SENT = "📨 Запит надіслано адміністратору. Я напишу, щойно його схвалять."
REQUEST_PENDING = "⏳ Запит ще розглядається."
REQUEST_REJECTED = "🚫 Доступ не надано."
SUBSCRIBED = "✅ Підписку підтверджено. Я повідомлятиму про світло, генератор і заряд батареї."
ALREADY_SUBSCRIBED = "✅ Ви вже підписані."
UNSUBSCRIBED = "👋 Підписку скасовано. Щоб повернутися, натисніть /start."
NOT_SUBSCRIBED = "Натисніть /start, щоб надіслати запит на доступ."
NO_DATA = "⏳ Ще немає даних від інвертора."
NO_SUBSCRIBERS = "Підписників немає."
APPROVED_MARK = "✅ Схвалено"
REJECTED_MARK = "❌ Відхилено"
ALREADY_DECIDED = "Запит уже оброблено"
NOT_FOUND = "Не знайдено"

_STATUS_ICONS = {SubscriberStatus.APPROVED: "✅", SubscriberStatus.PENDING: "⏳", SubscriberStatus.REJECTED: "🚫"}


def format_duration(duration: timedelta) -> str:
    minutes = int(duration.total_seconds() // 60)
    if minutes < 1:
        return "менше хвилини"
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} год")
    if mins and not days:
        parts.append(f"{mins} хв")
    return " ".join(parts)


def format_power(watts: int) -> str:
    watts = abs(watts)
    return f"{watts} Вт" if watts < 1000 else f"{watts / 1000:.1f} кВт"


def _clock(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def event_text(event: Event, tz: tzinfo) -> str | None:
    time = _clock(event.at, tz)
    match event.type:
        case EventType.GRID_LOST:
            return (
                f"🔴 <b>{time} Світло зникло</b>\n"
                f"🕓 Воно було {format_duration(event.previous_duration)}\n"
                f"🔋 Заряд {event.soc}%"
            )
        case EventType.GRID_RESTORED:
            text = f"🟢 <b>{time} Світло з'явилось</b>\n🕓 Його не було {format_duration(event.previous_duration)}"
            if event.gen_was_running:
                text += "\n⛽ Генератор можна вимикати"
            return text
        case EventType.BATTERY_LOW:
            return f"🪫 <b>Залишилось {event.soc}%</b>\n⛽ Час заводити генератор"
        case EventType.BATTERY_CRITICAL:
            return f"🚨 <b>Залишилось {event.soc}%</b>\n⛽ Терміново заведіть генератор"
        case EventType.GEN_CHARGED:
            return f"🔋 <b>Заряджено {event.soc}%</b>\n⛽ Генератор можна вимикати"
        case EventType.DONGLE_DOWN:
            return f"⚠️ Немає зв'язку з інвертором з {time}"
        case EventType.DONGLE_UP:
            return f"✅ Зв'язок з інвертором відновлено о {time}"
        case _:
            return None


def status_text(snapshot: Snapshot | None, state: State | None, now: datetime, tz: tzinfo) -> str:
    if snapshot is None or state is None:
        return NO_DATA
    if state.grid.value:
        grid = f"є · {snapshot.grid_v:.0f} В"
    else:
        grid = f"немає вже {format_duration(now - state.grid.since)}"
    # The latest reading disagrees with the debounced state: a change may be under way.
    if snapshot.grid_present_given(state.grid.value) != state.grid.value:
        grid += " (перевіряю)"
    if snapshot.battery_w > 0:
        flow = f" · заряджається {format_power(snapshot.battery_w)}"
    elif snapshot.battery_w < 0:
        flow = f" · розряджається {format_power(snapshot.battery_w)}"
    else:
        flow = ""
    lines = [
        f"⚡ Мережа: {grid}",
        f"⛽ Генератор: {'працює' if state.gen.value else 'не працює'}",
        f"🔋 Батарея: {snapshot.soc}%{flow}",
        f"🏠 Споживання: {format_power(snapshot.load_w)}",
        f"☀️ Сонце: {format_power(snapshot.pv_w)}",
        f"🕐 Оновлено {snapshot.taken_at.astimezone(tz):%H:%M:%S}",
    ]
    if now - snapshot.taken_at > STALE_AFTER:
        lines.append("⚠️ Дані застарілі: немає зв'язку з інвертором")
    return "\n".join(lines)


def _who(name: str, username: str | None) -> str:
    return escape(name) + (f" @{escape(username)}" if username else "")


def admin_request_text(name: str, username: str | None, chat_id: int) -> str:
    return f"🆕 Новий запит на доступ\n{_who(name, username)}\nID: {chat_id}"


# Names the user: the same mark is appended to a request card and to the /subscribers list.
def decision_mark(approve: bool, name: str) -> str:
    return f"{APPROVED_MARK if approve else REJECTED_MARK}: {escape(name)}"


def subscribers_text(subscribers: list[Subscriber]) -> str:
    lines = ["👥 Підписники:"]
    lines += [f"{_STATUS_ICONS.get(s.status, '•')} {_who(s.name, s.username)}" for s in subscribers]
    return "\n".join(lines)


# Not escaped: shown in a plain-text callback toast, unlike the other HTML texts.
def removed_text(name: str) -> str:
    return f"Видалено: {name}"
