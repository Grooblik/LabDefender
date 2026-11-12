from enum import StrEnum
from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from labdefender.enums import Role
from labdefender.keyboards.groups import create_group_button
from labdefender.web import mini_app_url


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
APP_LABEL = "📱 Открыть приложение"
NAME_LABEL = "✏️ Моё имя"
NAME_RESET_LABEL = "Вернуть имя из Telegram"


class MenuCallback(CallbackData, prefix="menu"):
    action: Literal["cancel", "id", "notifications", "grant", "name", "name_reset"]


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


def name_keyboard(*, chosen: bool) -> InlineKeyboardMarkup:
    """Under the name prompt: back to the Telegram name (when another is set) and «Отмена»."""
    rows = []
    if chosen:
        rows.append(
            [
                InlineKeyboardButton(
                    text=NAME_RESET_LABEL, callback_data=MenuCallback(action="name_reset").pack()
                )
            ]
        )
    rows.extend(cancel_keyboard().inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def app_button(webapp_url: str) -> InlineKeyboardButton:
    # The address travels inside the message, so it is always the current one — unlike the
    # menu button, which Telegram updates with a delay after a restart.
    return InlineKeyboardButton(text=APP_LABEL, web_app=WebAppInfo(url=mini_app_url(webapp_url)))


def app_keyboard(webapp_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[app_button(webapp_url)]])


def help_keyboard(role: Role, *, admin: bool, webapp_url: str = "") -> InlineKeyboardMarkup:
    rows = [[app_button(webapp_url)]] if webapp_url else []
    rows.append(
        [
            InlineKeyboardButton(
                text="🆔 Мой Telegram ID", callback_data=MenuCallback(action="id").pack()
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text=NAME_LABEL, callback_data=MenuCallback(action="name").pack())]
    )
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
