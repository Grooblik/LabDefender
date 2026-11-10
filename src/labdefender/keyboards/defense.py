from typing import Literal

from aiogram.filters.callback_data import CallbackData
from pydantic import Field

from labdefender.keyboards.coursework import selection_keyboard


class TeacherQueueCallback(CallbackData, prefix="teacher"):
    section: Literal["queue"] = "queue"
    action: Literal[
        "groups_new", "groups_view", "assignments_new", "assignments_view", "new", "days", "open"
    ]
    object_id: int = Field(ge=0, le=2**63 - 1)
    offset: int = Field(default=0, ge=0, le=2**31 - 1)


class StudentQueueCallback(CallbackData, prefix="student"):
    section: Literal["queue"] = "queue"
    action: Literal["groups", "assignments", "days", "choose", "move", "status"]
    object_id: int = Field(ge=0, le=2**63 - 1)
    offset: int = Field(default=0, ge=0, le=2**31 - 1)


class TeacherDayDraftCallback(CallbackData, prefix="teacher"):
    section: Literal["day_confirm"] = "day_confirm"
    action: Literal["confirm", "cancel"]
    nonce: str = Field(min_length=16, max_length=16, pattern=r"^[a-f0-9]+$")


class StudentQueueDraftCallback(CallbackData, prefix="student"):
    section: Literal["queue_confirm"] = "queue_confirm"
    action: Literal["confirm", "cancel"]
    nonce: str = Field(min_length=16, max_length=16, pattern=r"^[a-f0-9]+$")


def queue_draft_keyboard(nonce: str, *, teacher: bool):
    factory = TeacherDayDraftCallback if teacher else StudentQueueDraftCallback
    return selection_keyboard(
        [
            ("Подтвердить", factory(action="confirm", nonce=nonce).pack()),
            ("Отмена", factory(action="cancel", nonce=nonce).pack()),
        ]
    )
