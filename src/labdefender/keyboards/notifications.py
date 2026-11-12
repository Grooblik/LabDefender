from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from labdefender.services.notifications import NoticeCategory, NotificationPreferences

CATEGORY_LABELS = {
    NoticeCategory.SUBMISSIONS: "новые решения на проверку",
    NoticeCategory.JOINS: "вступление студентов в группы",
    NoticeCategory.QUEUE: "запись и смена слотов защиты",
}
MUTE_LABEL = "🔕 Не присылать такие"
APP_NOTICE_LABEL = "📱 Открыть в приложении"


class TeacherNotifyCallback(CallbackData, prefix="teacher"):
    section: Literal["notify"] = "notify"
    category: Literal["submissions", "joins", "queue", "all"]
    enabled: bool
    # "notice": the mute button under a notification; "settings": the /notifications screen.
    source: Literal["notice", "settings"] = "settings"


def notice_keyboard(
    buttons: list[tuple[str, str]],
    *,
    app: str | None = None,
    mute: NoticeCategory | None = None,
) -> InlineKeyboardMarkup:
    """Chat buttons first, then the same screen in the Mini App (`app` address), then mute."""
    rows = [[InlineKeyboardButton(text=label, callback_data=data)] for label, data in buttons]
    if app:
        rows.append([InlineKeyboardButton(text=APP_NOTICE_LABEL, web_app=WebAppInfo(url=app))])
    if mute is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=MUTE_LABEL,
                    callback_data=TeacherNotifyCallback(
                        category=mute.value, enabled=False, source="notice"
                    ).pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(preferences: NotificationPreferences) -> str:
    lines = ["Уведомления преподавателя:"]
    for category, label in CATEGORY_LABELS.items():
        mark = "✅" if preferences.enabled(category) else "🔕"
        lines.append(f"{mark} {label[:1].upper()}{label[1:]}")
    lines.append("")
    lines.append(
        "Кнопки ниже включают и выключают уведомления. Студентам уведомления приходят всегда."
    )
    return "\n".join(lines)


def settings_keyboard(preferences: NotificationPreferences) -> InlineKeyboardMarkup:
    rows = []
    for category, label in CATEGORY_LABELS.items():
        enabled = preferences.enabled(category)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'Выключить' if enabled else 'Включить'}: {label}",
                    callback_data=TeacherNotifyCallback(
                        category=category.value, enabled=not enabled
                    ).pack(),
                )
            ]
        )
    states = [preferences.enabled(category) for category in CATEGORY_LABELS]
    bulk = []
    if any(states):
        bulk.append(
            InlineKeyboardButton(
                text="Выключить все",
                callback_data=TeacherNotifyCallback(category="all", enabled=False).pack(),
            )
        )
    if not all(states):
        bulk.append(
            InlineKeyboardButton(
                text="Включить все",
                callback_data=TeacherNotifyCallback(category="all", enabled=True).pack(),
            )
        )
    rows.append(bulk)
    return InlineKeyboardMarkup(inline_keyboard=rows)
