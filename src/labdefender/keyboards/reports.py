from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton
from pydantic import Field


class ReportCallback(CallbackData, prefix="report"):
    """group: a group's table (object_id = group); groups: a page of groups (object_id =
    offset) to choose from; me: the student's own report."""

    kind: Literal["group", "groups", "me"]
    object_id: int = Field(default=0, ge=0, le=2**63 - 1)


def group_report_button(group_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="📊 Отчёт по группе",
        callback_data=ReportCallback(kind="group", object_id=group_id).pack(),
    )


def my_report_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="📊 Мой отчёт", callback_data=ReportCallback(kind="me").pack())
