"""Buttons of the teacher's slot wizard: a calendar of 2-3 weeks and time/capacity presets.

Pure functions only: callers pass Moscow "now", busy intervals and the draft nonce.
Every button is optional — each wizard step also accepts the same value as text.
The grid and calendar rules themselves live in `services.slot_grid`.
"""

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import Field

from labdefender.keyboards.defense import TeacherDayDraftCallback
from labdefender.services.slot_grid import (
    CAPACITIES,
    WEEKDAYS,
    calendar_pages,
    duration_label,
    from_minutes,
)


class TeacherSlotCallback(CallbackData, prefix="teacher"):
    section: Literal["slot"] = "slot"
    action: Literal[
        "page", "date", "start", "duration", "capacity", "repeat", "back", "noop", "cancel"
    ]
    # page index, YYYYMMDD, minutes since midnight, duration, capacity or template slot ID.
    value: int = Field(default=0, ge=0, le=2**63 - 1)
    nonce: str = Field(min_length=16, max_length=16, pattern=r"^[a-f0-9]+$")


def date_value(value: date) -> int:
    return value.year * 10000 + value.month * 100 + value.day


def parse_date_value(value: int) -> date:
    return date(value // 10000, value // 100 % 100, value % 100)


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
