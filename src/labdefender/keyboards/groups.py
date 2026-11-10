from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import Field

from labdefender.enums import Role
from labdefender.keyboards.coursework import StudentCourseCallback, TeacherCourseCallback
from labdefender.services.groups import GROUP_PAGE_SIZE, GroupPage


class TeacherGroupCallback(CallbackData, prefix="teacher"):
    section: Literal["groups"] = "groups"
    action: Literal["open", "invite", "list", "invite_list"]
    value: int = Field(ge=0, le=2**63 - 1)


class StudentGroupCallback(CallbackData, prefix="student"):
    section: Literal["groups"] = "groups"
    action: Literal["open", "list"]
    value: int = Field(ge=0, le=2**63 - 1)


class GroupCreationCallback(CallbackData, prefix="teacher"):
    section: Literal["create_group"] = "create_group"
    action: Literal["confirm", "cancel"]
    nonce: str = Field(min_length=16, max_length=16, pattern=r"^[a-f0-9]+$")


def group_list_keyboard(
    page: GroupPage, *, role: Role, invite: bool = False
) -> InlineKeyboardMarkup:
    callback_type = TeacherGroupCallback if role == Role.TEACHER else StudentGroupCallback
    action = "invite" if invite and role == Role.TEACHER else "open"
    page_action = "invite_list" if invite and role == Role.TEACHER else "list"
    rows = [
        [
            InlineKeyboardButton(
                text=f"{group.name[:60]} · #{group.id}",
                callback_data=callback_type(action=action, value=group.id).pack(),
            )
        ]
        for group in page.groups
    ]
    navigation = []
    if page.offset:
        navigation.append(
            InlineKeyboardButton(
                text="← Назад",
                callback_data=callback_type(
                    action=page_action, value=max(0, page.offset - GROUP_PAGE_SIZE)
                ).pack(),
            )
        )
    if page.has_next:
        navigation.append(
            InlineKeyboardButton(
                text="Далее →",
                callback_data=callback_type(
                    action=page_action, value=page.offset + GROUP_PAGE_SIZE
                ).pack(),
            )
        )
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teacher_group_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Новое задание",
                    callback_data=TeacherCourseCallback(
                        action="publish", object_id=group_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проверить решения",
                    callback_data=TeacherCourseCallback(
                        action="review_list", object_id=group_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="Ссылка-приглашение",
                    callback_data=TeacherGroupCallback(action="invite", value=group_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="Мои группы",
                    callback_data=TeacherGroupCallback(action="list", value=0).pack(),
                )
            ],
        ]
    )


def student_assignments_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Задания группы",
                    callback_data=StudentCourseCallback(action="list", object_id=group_id).pack(),
                )
            ]
        ]
    )


def student_group_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть группу",
                    callback_data=StudentGroupCallback(action="open", value=group_id).pack(),
                )
            ],
        ]
    )


def group_confirmation_keyboard(nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать",
                    callback_data=GroupCreationCallback(action="confirm", nonce=nonce).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=GroupCreationCallback(action="cancel", nonce=nonce).pack(),
                )
            ],
        ]
    )
