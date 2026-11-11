from enum import StrEnum
from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from labdefender.enums import Role
from labdefender.keyboards.groups import create_group_button


class MenuButton(StrEnum):
    """Texts of the main reply keyboard. Each button opens the same screen as its command."""

    ASSIGNMENTS = "📚 Задания"
    QUEUE = "🗓 Защита и очередь"
    GROUPS = "👥 Мои группы"
    HELP = "❓ Помощь"
    REVIEW = "📥 На проверку"
    NEW_ASSIGNMENT = "📝 Новое задание"
    NEW_SLOT = "🕒 Новый слот"
    INVITE = "🔗 Пригласить"
    NOTIFICATIONS = "🔔 Уведомления"


MENU_COMMANDS = {
    MenuButton.ASSIGNMENTS: "assignments",
    MenuButton.QUEUE: "queue",
    MenuButton.GROUPS: "my_groups",
    MenuButton.HELP: "help",
    MenuButton.REVIEW: "submissions",
    MenuButton.NEW_ASSIGNMENT: "new_assignment",
    MenuButton.NEW_SLOT: "new_slot",
    MenuButton.INVITE: "invite",
    MenuButton.NOTIFICATIONS: "notifications",
}

MENU_LAYOUT = {
    Role.STUDENT: (
        (MenuButton.ASSIGNMENTS, MenuButton.QUEUE),
        (MenuButton.GROUPS, MenuButton.HELP),
    ),
    Role.TEACHER: (
        (MenuButton.REVIEW, MenuButton.QUEUE),
        (MenuButton.NEW_ASSIGNMENT, MenuButton.NEW_SLOT),
        (MenuButton.GROUPS, MenuButton.INVITE),
        (MenuButton.NOTIFICATIONS, MenuButton.HELP),
    ),
}

MENU_TEXTS = frozenset(button.value for button in MenuButton)

GRANT_LABEL = "👤 Выдать роль преподавателя"


class MenuCallback(CallbackData, prefix="menu"):
    action: Literal["cancel", "id", "notifications", "grant"]


def main_menu(role: Role) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=button.value) for button in row] for row in MENU_LAYOUT[role]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def menu_sections(role: Role) -> list[str]:
    return [
        f"{button.value} — /{MENU_COMMANDS[button]}" for row in MENU_LAYOUT[role] for button in row
    ]


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Under a text prompt: works like /cancel for whatever input is currently awaited."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отмена", callback_data=MenuCallback(action="cancel").pack()
                )
            ]
        ]
    )


def help_keyboard(role: Role, *, admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🆔 Мой Telegram ID", callback_data=MenuCallback(action="id").pack()
            )
        ]
    ]
    if role == Role.STUDENT:
        rows.append(
            [
                InlineKeyboardButton(
                    text=MenuButton.NOTIFICATIONS.value,
                    callback_data=MenuCallback(action="notifications").pack(),
                )
            ]
        )
        rows.append([InlineKeyboardButton(text="🎓 Я преподаватель", callback_data="auth:teacher")])
    else:
        rows.append([create_group_button()])
    if admin:
        rows.append(
            [
                InlineKeyboardButton(
                    text=GRANT_LABEL, callback_data=MenuCallback(action="grant").pack()
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
