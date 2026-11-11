"""Slot grid and calendar rules shared by the chat wizard and the Mini App API.

Pure functions only: callers pass Moscow "now" and the busy intervals of the day.
"""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

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


def to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def from_minutes(value: int) -> time:
    if not 0 <= value < MINUTES_PER_DAY:
        raise ValueError
    return time(value // 60, value % 60)


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
