from typing import Literal

from aiogram.filters.callback_data import CallbackData
from pydantic import Field

from labdefender.keyboards.coursework import selection_keyboard


class TeacherQueueCallback(CallbackData, prefix="teacher"):
    """object_id: a group for groups/slots/create/unbooked, a slot for open/close/reopen/cancel.

    `assignments_*`, `new` and `days` come from buttons sent before slots belonged to groups
    (`new`/`days` carry an assignment); they still lead to the matching group.
    """

    section: Literal["queue"] = "queue"
    action: Literal[
        "groups_new",
        "groups_view",
        "slots",
        "create",
        "unbooked",
        "open",
        "close",
        "reopen",
        "cancel",
        "assignments_new",
        "assignments_view",
        "new",
        "days",
    ]
    object_id: int = Field(ge=0, le=2**63 - 1)
    offset: int = Field(default=0, ge=0, le=2**31 - 1)


class StudentQueueCallback(CallbackData, prefix="student"):
    """object_id: a group for assignments (the student's works)/slots, a slot for choose,
    an assignment for days/pick/move/status; `pick` also carries the chosen slot."""

    section: Literal["queue"] = "queue"
    action: Literal["groups", "assignments", "slots", "days", "choose", "pick", "move", "status"]
    object_id: int = Field(ge=0, le=2**63 - 1)
    offset: int = Field(default=0, ge=0, le=2**31 - 1)
    slot_id: int = Field(default=0, ge=0, le=2**63 - 1)


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
