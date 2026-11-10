from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import Field


class TeacherCourseCallback(CallbackData, prefix="teacher"):
    section: Literal["course"] = "course"
    action: Literal[
        "groups_publish",
        "groups_review",
        "publish",
        "review_list",
        "attempts",
        "open",
        "approve",
        "return",
    ]
    object_id: int = Field(ge=0, le=2**63 - 1)
    offset: int = Field(default=0, ge=0, le=2**31 - 1)


class StudentCourseCallback(CallbackData, prefix="student"):
    section: Literal["course"] = "course"
    action: Literal["groups", "list", "open", "submit", "history", "attempt"]
    object_id: int = Field(ge=0, le=2**63 - 1)
    offset: int = Field(default=0, ge=0, le=2**31 - 1)


class AssignmentFormatCallback(CallbackData, prefix="teacher"):
    section: Literal["assignment_format"] = "assignment_format"
    action: Literal["online", "in_person", "cancel"]


class TeacherDraftCallback(CallbackData, prefix="teacher"):
    section: Literal["publish"] = "publish"
    action: Literal["confirm", "cancel"]
    nonce: str = Field(min_length=16, max_length=16, pattern=r"^[a-f0-9]+$")


class StudentDraftCallback(CallbackData, prefix="student"):
    section: Literal["submit"] = "submit"
    action: Literal["confirm", "cancel"]
    nonce: str = Field(min_length=16, max_length=16, pattern=r"^[a-f0-9]+$")


def assignment_format_keyboard() -> InlineKeyboardMarkup:
    return selection_keyboard(
        [
            (
                "Очная сдача",
                AssignmentFormatCallback(action="in_person").pack(),
            ),
            (
                "Онлайн-сдача",
                AssignmentFormatCallback(action="online").pack(),
            ),
            (
                "Отмена",
                AssignmentFormatCallback(action="cancel").pack(),
            ),
        ]
    )


def selection_keyboard(
    buttons: list[tuple[str, str]], *, previous: str | None = None, following: str | None = None
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label[:64], callback_data=data)] for label, data in buttons]
    navigation = []
    if previous:
        navigation.append(InlineKeyboardButton(text="← Назад", callback_data=previous))
    if following:
        navigation.append(InlineKeyboardButton(text="Далее →", callback_data=following))
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def draft_keyboard(nonce: str, *, teacher: bool) -> InlineKeyboardMarkup:
    factory = TeacherDraftCallback if teacher else StudentDraftCallback
    return selection_keyboard(
        [
            (
                "Опубликовать" if teacher else "Отправить",
                factory(action="confirm", nonce=nonce).pack(),
            ),
            ("Отмена", factory(action="cancel", nonce=nonce).pack()),
        ]
    )
