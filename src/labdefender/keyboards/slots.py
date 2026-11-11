"""Buttons of the teacher's slot wizard: a calendar of 2-3 weeks and time/capacity presets.

Pure functions only: callers pass Moscow "now", busy intervals and the draft nonce.
Every button is optional — each wizard step also accepts the same value as text.
"""

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import Field

from labdefender.keyboards.defense import TeacherDayDraftCallback

CALENDAR_ROWS = 3
CALENDAR_MIN_DAYS = 14
# Pages are offered while they start within a year; farther dates can be typed as YYYY-MM-DD.
CALENDAR_HORIZON_DAYS = 365
GRID_FIRST_START = 8 * 60
GRID_LAST_START = 20 * 60 + 30
GRID_STEP = 30
DURATIONS = (30, 60, 90, 120, 180, 240)
CAPACITIES = (5, 10, 15, 20, 25, 30)
MINUTES_PER_DAY = 24 * 60

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

Busy = Sequence[tuple[time, time]]


class TeacherSlotCallback(CallbackData, prefix="teacher"):
    section: Literal["slot"] = "slot"
    action: Literal[
        "page", "date", "start", "duration", "capacity", "repeat", "back", "noop", "cancel"
    ]
    # page index, YYYYMMDD, minutes since midnight, duration, capacity or template slot ID.
    value: int = Field(default=0, ge=0, le=2**63 - 1)
    nonce: str = Field(min_length=16, max_length=16, pattern=r"^[a-f0-9]+$")


def to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def from_minutes(value: int) -> time:
    if not 0 <= value < MINUTES_PER_DAY:
        raise ValueError
    return time(value // 60, value % 60)


def date_value(value: date) -> int:
    return value.year * 10000 + value.month * 100 + value.day


def parse_date_value(value: int) -> date:
    return date(value // 10000, value // 100 % 100, value % 100)


def weekday_label(value: date) -> str:
    return WEEKDAYS[value.weekday()].lower()


def duration_label(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} мин"
    if not rest:
        return f"{hours} ч"
    if rest == 30:
        return f"{hours},5 ч"
    return f"{hours} ч {rest} мин"


def interval_label(start: time, end: time) -> str:
    return f"{start:%H:%M}–{end:%H:%M}"


def window_length(first: date) -> int:
    """Days on a page starting at `first`: three week rows, where the days of the first week
    before `first` are dots and the last row ends with as many dots, but never under 14 dates."""
    leading = first.weekday()
    trailing = min(leading, 7 * CALENDAR_ROWS - CALENDAR_MIN_DAYS - leading)
    return 7 * CALENDAR_ROWS - leading - trailing


def calendar_pages(today: date) -> list[tuple[date, date]]:
    """Consecutive (first, last) pages from today; each page starts after the previous one."""
    pages, first = [], today
    horizon = today + timedelta(days=CALENDAR_HORIZON_DAYS)
    while first <= horizon:
        last = first + timedelta(days=window_length(first) - 1)
        pages.append((first, last))
        first = last + timedelta(days=1)
    return pages


def calendar_window(today: date, page: int) -> tuple[date, date]:
    pages = calendar_pages(today)
    if not 0 <= page < len(pages):
        raise ValueError
    return pages[page]


def range_label(first: date, last: date) -> str:
    if first.year != last.year:
        return (
            f"{first.day} {MONTHS_GENITIVE[first.month - 1]} {first.year} – "
            f"{last.day} {MONTHS_GENITIVE[last.month - 1]} {last.year}"
        )
    if first.month != last.month:
        return (
            f"{first.day} {MONTHS_GENITIVE[first.month - 1]} – "
            f"{last.day} {MONTHS_GENITIVE[last.month - 1]} {last.year}"
        )
    return f"{first.day}–{last.day} {MONTHS_GENITIVE[last.month - 1]} {last.year}"


def _overlaps(start: int, end: int, busy: Busy) -> bool:
    return any(
        start < to_minutes(other_end) and end > to_minutes(other_start)
        for other_start, other_end in busy
    )


def _not_finished(day: date, end: int, now: datetime) -> bool:
    if day != now.date():
        return day > now.date()
    return end > now.hour * 60 + now.minute


def duration_options(day: date, start: int, busy: Busy, now: datetime) -> list[tuple[int, int]]:
    """(duration, end) pairs ending the same day, in the future and without overlaps."""
    options = []
    for duration in DURATIONS:
        end = start + duration
        if end >= MINUTES_PER_DAY or _overlaps(start, end, busy):
            continue
        if _not_finished(day, end, now):
            options.append((duration, end))
    return options


def start_options(day: date, busy: Busy, now: datetime) -> list[int]:
    """Grid starts that are still ahead today and leave at least one preset duration."""
    current = now.hour * 60 + now.minute if day == now.date() else -1
    return [
        start
        for start in range(GRID_FIRST_START, GRID_LAST_START + 1, GRID_STEP)
        if start > current and duration_options(day, start, busy, now)
    ]


def template_fits(day: date, start: time, end: time, busy: Busy, now: datetime) -> bool:
    return (
        start < end
        and not _overlaps(to_minutes(start), to_minutes(end), busy)
        and _not_finished(day, to_minutes(end), now)
    )


def _button(text: str, action: str, nonce: str, value: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=TeacherSlotCallback(action=action, value=value, nonce=nonce).pack(),
    )


def _rows(buttons: list[InlineKeyboardButton], width: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[index : index + width] for index in range(0, len(buttons), width)]


def _footer(nonce: str, *, back: bool = True) -> list[InlineKeyboardButton]:
    row = [_button("← Назад", "back", nonce)] if back else []
    row.append(_button("Отмена", "cancel", nonce))
    return row


def calendar_keyboard(
    *, today: date, page: int, marked: Iterable[date], nonce: str
) -> InlineKeyboardMarkup:
    pages = calendar_pages(today)
    if not 0 <= page < len(pages):
        raise ValueError
    first, last = pages[page]
    marked = set(marked)
    rows = [[_button(name, "noop", nonce) for name in WEEKDAYS]]
    cursor = first - timedelta(days=first.weekday())
    while cursor <= last:
        row = []
        for _ in range(7):
            if first <= cursor <= last:
                label = f"{cursor.day}.{cursor.month:02d}" if cursor.day == 1 else str(cursor.day)
                if cursor in marked:
                    label += "•"
                row.append(_button(label, "date", nonce, date_value(cursor)))
            else:
                row.append(_button("·", "noop", nonce))
            cursor += timedelta(days=1)
        rows.append(row)
    navigation = []
    if page > 0:
        navigation.append(_button("← Раньше", "page", nonce, page - 1))
    if page < len(pages) - 1:
        navigation.append(_button("Позже →", "page", nonce, page + 1))
    rows.append(navigation)
    rows.append(_footer(nonce, back=False))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_keyboard(
    options: Sequence[int], *, template: tuple[int, str] | None, nonce: str
) -> InlineKeyboardMarkup:
    rows = _rows(
        [_button(f"{from_minutes(start):%H:%M}", "start", nonce, start) for start in options], 4
    )
    if template is not None:
        slot_id, label = template
        rows.append([_button(label, "repeat", nonce, slot_id)])
    rows.append(_footer(nonce))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def duration_keyboard(options: Sequence[tuple[int, int]], *, nonce: str) -> InlineKeyboardMarkup:
    rows = _rows(
        [
            _button(
                f"{duration_label(duration)} · до {from_minutes(end):%H:%M}",
                "duration",
                nonce,
                duration,
            )
            for duration, end in options
        ],
        2,
    )
    rows.append(_footer(nonce))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def capacity_keyboard(*, nonce: str) -> InlineKeyboardMarkup:
    rows = _rows([_button(str(value), "capacity", nonce, value) for value in CAPACITIES], 3)
    rows.append(_footer(nonce))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slot_confirmation_keyboard(*, nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить",
                    callback_data=TeacherDayDraftCallback(action="confirm", nonce=nonce).pack(),
                )
            ],
            [
                _button("← Назад", "back", nonce),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=TeacherDayDraftCallback(action="cancel", nonce=nonce).pack(),
                ),
            ],
        ]
    )
