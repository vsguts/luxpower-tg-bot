from datetime import date, datetime, time, timedelta, tzinfo
from html import escape

from lxbot.luxpower.registers import Snapshot
from lxbot.monitor.detector import Event, EventType, State
from lxbot.schedule.compare import MatchKind, ScheduleMatch, current_outage, next_outage
from lxbot.schedule.model import DaySchedule, Outage, Schedule
from lxbot.storage.db import Subscriber, SubscriberStatus

SNAPSHOT_STALE_AFTER = timedelta(minutes=2)
ON_TIME = timedelta(minutes=2)

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
SCHEDULE_TOMORROW_PENDING = "⏳ Графік на завтра ще не опубліковано"
SCHEDULE_SOURCE_UP = "✅ Графік відключень знову оновлюється"

_WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя")
_MONTHS = (
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
)

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


# A local-midnight end closes the day it ends, as on DTEK's own schedule: "до 24:00".
def _range_end(outage: Outage, tz: tzinfo) -> str:
    end = outage.end.astimezone(tz)
    if end.time() == time(0) and end.date() > outage.start.astimezone(tz).date():
        return "24:00"
    return f"{end:%H:%M}"


def format_range(outage: Outage, tz: tzinfo) -> str:
    return f"з {_clock(outage.start, tz)} до {_range_end(outage, tz)}"


def _approx_hours(duration: timedelta) -> str:
    half_hours = round(duration.total_seconds() / 1800)
    return f"~{half_hours / 2:g} год"


def format_deviation(deviation: timedelta) -> str:
    if abs(deviation) <= ON_TIME:
        return "вчасно"
    direction = "пізніше" if deviation > timedelta() else "раніше"
    return f"на {format_duration(abs(deviation))} {direction}"


def _day_word(moment: datetime, now: datetime, tz: tzinfo) -> str:
    days = (moment.astimezone(tz).date() - now.astimezone(tz).date()).days
    return {0: "сьогодні", 1: "завтра"}.get(days, f"{moment.astimezone(tz):%d.%m}")


def _upcoming_line(outage: Outage, now: datetime, tz: tzinfo) -> str:
    kind = "Наступне відключення" if outage.certain else "Можливе відключення"
    return f"📅 {kind}: {_day_word(outage.start, now, tz)} {format_range(outage, tz)}"


def schedule_lines(match: ScheduleMatch, at: datetime, tz: tzinfo) -> list[str]:
    interval = match.interval
    if interval is None:
        lines = ["⚠️ Не за графіком"]
    else:
        planned = "За графіком" if interval.certain else "За можливим відключенням"
        deviation = format_deviation(match.deviation)
        if match.kind is MatchKind.LOST:
            lines = [
                f"⏱ {planned} з {_clock(interval.start, tz)} — {deviation}",
                f"⏳ Очікуємо світло о {_clock(interval.end, tz)}",
            ]
        else:
            lines = [f"⏱ {planned} до {_range_end(interval, tz)} — {deviation}"]
    if match.kind is MatchKind.RESTORED:
        if match.next_outage is None:
            lines.append("📅 Відключень за графіком більше не заплановано")
        else:
            lines.append(_upcoming_line(match.next_outage, at, tz))
    return lines


def _date_title(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {day.day} {_MONTHS[day.month - 1]}"


def day_schedule_text(title: str, schedule: DaySchedule, tz: tzinfo, icon: str = "📅") -> str:
    lines = [f"{icon} <b>{title}</b> · {_date_title(schedule.day)} · черга {schedule.label}"]
    if not schedule.outages:
        lines.append("✅ Відключень не заплановано")
        return "\n".join(lines)
    for outage in schedule.outages:
        if outage.certain:
            lines.append(f"🕐 {format_range(outage, tz)} ({_approx_hours(outage.duration)})")
        else:
            lines.append(f"❔ можливо {format_range(outage, tz)}")
    if schedule.certain_total:
        lines.append(f"Без світла: {format_duration(schedule.certain_total)}")
    return "\n".join(lines)


def schedule_status_line(schedule: Schedule, now: datetime, tz: tzinfo) -> str:
    timeline = schedule.timeline()
    current = current_outage(timeline, now)
    if current is not None:
        prefix = "📅 За графіком світла немає до " if current.certain else "📅 Можливе відключення до "
        return prefix + _range_end(current, tz)
    upcoming = next_outage(timeline, now)
    if upcoming is None:
        return "📅 Відключень за графіком не заплановано"
    return _upcoming_line(upcoming, now, tz)


def event_text(event: Event, tz: tzinfo) -> str | None:
    clock = _clock(event.at, tz)
    match event.type:
        case EventType.GRID_LOST:
            text = (
                f"🔴 <b>{clock} Світло зникло</b>\n"
                f"🕓 Воно було {format_duration(event.previous_duration)}\n"
                f"🔋 Заряд {event.soc}%"
            )
            return _with_schedule(text, event, tz)
        case EventType.GRID_RESTORED:
            text = f"🟢 <b>{clock} Світло з'явилось</b>\n🕓 Його не було {format_duration(event.previous_duration)}"
            if event.gen_was_running:
                text += "\n⛽ Генератор можна вимикати"
            return _with_schedule(text, event, tz)
        case EventType.BATTERY_LOW:
            return f"🪫 <b>Залишилось {event.soc}%</b>\n⛽ Час заводити генератор"
        case EventType.BATTERY_CRITICAL:
            return f"🚨 <b>Залишилось {event.soc}%</b>\n⛽ Терміново заведіть генератор"
        case EventType.GEN_CHARGED:
            return f"🔋 <b>Заряджено {event.soc}%</b>\n⛽ Генератор можна вимикати"
        case EventType.DONGLE_DOWN:
            return f"⚠️ Немає зв'язку з інвертором з {clock}"
        case EventType.DONGLE_UP:
            return f"✅ Зв'язок з інвертором відновлено о {clock}"
        case EventType.SCHEDULE_TODAY:
            return day_schedule_text("Графік на сьогодні", event.schedule, tz)
        case EventType.SCHEDULE_TOMORROW:
            return day_schedule_text("Графік на завтра", event.schedule, tz)
        case EventType.SCHEDULE_TOMORROW_PUBLISHED:
            return day_schedule_text("Графік на завтра опубліковано", event.schedule, tz)
        case EventType.SCHEDULE_TOMORROW_PENDING:
            return SCHEDULE_TOMORROW_PENDING
        case EventType.SCHEDULE_CHANGED:
            word = _day_word(datetime.combine(event.schedule.day, time(12), tz), event.at, tz)
            return day_schedule_text(f"Графік на {word} змінився", event.schedule, tz, icon="🔄")
        case EventType.SCHEDULE_SOURCE_DOWN:
            return f"⚠️ Графік відключень не оновлюється з {clock}"
        case EventType.SCHEDULE_SOURCE_UP:
            return SCHEDULE_SOURCE_UP
        case _:
            return None


def _with_schedule(text: str, event: Event, tz: tzinfo) -> str:
    if event.match is None:
        return text
    return "\n".join([text, *schedule_lines(event.match, event.at, tz)])


def status_text(
    snapshot: Snapshot | None, state: State | None, now: datetime, tz: tzinfo, schedule: Schedule | None = None
) -> str:
    schedule_line = schedule_status_line(schedule, now, tz) if schedule is not None else None
    if snapshot is None or state is None:
        return f"{NO_DATA}\n{schedule_line}" if schedule_line else NO_DATA
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
    ]
    if schedule_line:
        lines.append(schedule_line)
    lines.append(f"🕐 Оновлено {snapshot.taken_at.astimezone(tz):%H:%M:%S}")
    if now - snapshot.taken_at > SNAPSHOT_STALE_AFTER:
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
