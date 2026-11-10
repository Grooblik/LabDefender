from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def role_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Студент", callback_data="auth:student")],
            [InlineKeyboardButton(text="Я преподаватель", callback_data="auth:teacher")],
        ]
    )
