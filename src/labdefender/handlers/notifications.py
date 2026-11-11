"""Notifications about events that reached the other side, and teacher notification settings.

Students always receive notices: new assignment, review result, a new or reopened defense slot.
Teachers receive new submissions, group joins and slot bookings, each category can be muted.

With the Mini App on, every notice also has a button that opens the same object in the app.

Delivery is best effort. Handlers call `notify_*` only after the service has committed, before
their own reply, so a failed reply does not lose the notice. Any error while preparing or
sending a notice is logged and never undoes or hides the action. Notices are not resent after
a restart: the cards in the bot remain the source of truth.
"""

import asyncio
import functools
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import Assignment, QueueEntry, StudyGroup, User
from labdefender.enums import Role, SubmissionKind, SubmissionStatus
from labdefender.handlers.coursework import format_label, split_text
from labdefender.handlers.defense import slot_time_label
from labdefender.keyboards.coursework import StudentCourseCallback, TeacherCourseCallback
from labdefender.keyboards.defense import StudentQueueCallback, TeacherQueueCallback
from labdefender.keyboards.groups import TeacherGroupCallback
from labdefender.keyboards.notifications import (
    CATEGORY_LABELS,
    TeacherNotifyCallback,
    notice_keyboard,
    settings_keyboard,
    settings_text,
)
from labdefender.services import queue as queue_service
from labdefender.services.notifications import (
    NoticeCategory,
    approved_unbooked_students,
    get_notification_preferences,
    group_students,
    set_notification_preferences,
    slot_context,
    submission_context,
    teacher_preferences,
)
from labdefender.services.slot_grid import weekday_label
from labdefender.web import mini_app_url

logger = logging.getLogger(__name__)

# How long a stopping bot waits for scheduled notices before cancelling them.
SHUTDOWN_WAIT_SECONDS = 10.0

KIND_LABELS = {
    SubmissionKind.PDF: "PDF",
    SubmissionKind.ZIP: "ZIP",
    SubmissionKind.TEXT: "текст",
    SubmissionKind.LINK: "ссылка",
}


@dataclass(frozen=True)
class Notice:
    chat_id: int
    text: str
    markup: InlineKeyboardMarkup | None = None


class Notifier:
    """Sends notices one by one.

    Inline mode (default; tests and tools) delivers inside the handler. Background mode, used
    by the running bot, schedules delivery so the acting user does not wait for a broadcast;
    `close()` lets scheduled notices finish at shutdown. With `webapp_url`, notices carry
    a button to the matching screen of the Mini App.
    """

    def __init__(
        self, *, background: bool = False, delay: float = 0.0, webapp_url: str = ""
    ) -> None:
        self.background = background
        self.delay = delay
        self.webapp_url = webapp_url
        self._tasks: set[asyncio.Task] = set()

    def app_url(self, screen: str) -> str | None:
        return mini_app_url(self.webapp_url, screen) if self.webapp_url else None

    async def send(self, bot: Bot, notices: Sequence[Notice]) -> None:
        notices = tuple(notices)
        if not notices:
            return
        if not self.background:
            await self._deliver(bot, notices)
            return
        task = asyncio.create_task(self._deliver(bot, notices))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def close(self) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=SHUTDOWN_WAIT_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning("Notifications were cancelled at shutdown: %d batches", len(pending))

    async def _deliver(self, bot: Bot, notices: tuple[Notice, ...]) -> None:
        for index, notice in enumerate(notices):
            if index and self.delay:
                await asyncio.sleep(self.delay)  # Stay well below Telegram's broadcast limit.
            chunks = split_text(notice.text)
            for number, chunk in enumerate(chunks):
                markup = notice.markup if number == len(chunks) - 1 else None
                if not await self._send_chunk(bot, notice.chat_id, chunk, markup):
                    break

    async def _send_chunk(
        self, bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None
    ) -> bool:
        for attempt in range(2):
            try:
                await bot.send_message(
                    chat_id,
                    text,
                    parse_mode=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    reply_markup=markup,
                )
                return True
            except TelegramRetryAfter as error:
                if attempt:
                    break
                await asyncio.sleep(error.retry_after)
            except TelegramAPIError as error:
                # Blocked bot, deleted chat or network failure. The action itself is saved.
                logger.info("Notification was not delivered: %s", type(error).__name__)
                return False
            except Exception:
                logger.exception("Notification failed unexpectedly")
                return False
        logger.info("Notification was not delivered: flood limit")
        return False


def _safe(function):
    @functools.wraps(function)
    async def wrapper(*args, **kwargs) -> None:
        try:
            await function(*args, **kwargs)
        except Exception:
            # Called after commit: a notification problem must never look like a failed action.
            logger.exception("Notification was not prepared")

    return wrapper


def _person(user: User) -> str:
    return f"{user.full_name} (@{user.username})" if user.username else user.full_name


def _slot_line(slot) -> str:
    return (
        f"Слот: {slot.date:%d.%m.%Y} ({weekday_label(slot.date)}) · "
        f"{slot_time_label(slot)} (Москва)"
    )


@_safe
async def notify_submission_received(
    notifier: Notifier, bot: Bot, session: AsyncSession, *, submission_id: int
) -> None:
    context = await submission_context(session, submission_id=submission_id)
    if context is None or context.teacher.role != Role.TEACHER:
        return
    preferences = await teacher_preferences(session, teacher_id=context.teacher.id)
    if not preferences.submissions:
        return
    submission = context.submission
    await notifier.send(
        bot,
        [
            Notice(
                context.teacher.telegram_id,
                "Новое решение на проверку.\n"
                f"Группа: {context.group.name}\n"
                f"Задание: {context.assignment.title}\n"
                f"Студент: {_person(context.student)}\n"
                f"Попытка №{submission.attempt_number} · {KIND_LABELS[submission.kind]}",
                notice_keyboard(
                    [
                        (
                            "Открыть попытку",
                            TeacherCourseCallback(action="open", object_id=submission.id).pack(),
                        )
                    ],
                    app=notifier.app_url(f"/review/s/{submission.id}"),
                    mute=NoticeCategory.SUBMISSIONS,
                ),
            )
        ],
    )


@_safe
async def notify_submission_reviewed(
    notifier: Notifier, bot: Bot, session: AsyncSession, *, submission_id: int
) -> None:
    context = await submission_context(session, submission_id=submission_id)
    if context is None or context.student.role != Role.STUDENT:
        return
    submission, assignment = context.submission, context.assignment
    header = f"Группа: {context.group.name}\nЗадание: {assignment.title}\n"
    header += f"Попытка №{submission.attempt_number}"
    if submission.status == SubmissionStatus.APPROVED:
        text = f"Работа допущена.\n{header}\nТеперь можно выбрать слот защиты."
        button = (
            "Выбрать слот защиты",
            StudentQueueCallback(action="days", object_id=assignment.id).pack(),
        )
        screen = f"/defense/a/{assignment.id}"
    elif submission.status == SubmissionStatus.RETURNED:
        text = (
            f"Работа возвращена на доработку.\n{header}\n"
            f"Комментарий преподавателя:\n{submission.review_comment}"
        )
        button = (
            "Открыть задание",
            StudentCourseCallback(action="open", object_id=assignment.id).pack(),
        )
        screen = f"/tasks/a/{assignment.id}"
    else:
        return
    markup = notice_keyboard([button], app=notifier.app_url(screen))
    await notifier.send(bot, [Notice(context.student.telegram_id, text, markup)])


@_safe
async def notify_assignment_published(
    notifier: Notifier, bot: Bot, session: AsyncSession, *, assignment_id: int
) -> None:
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return
    group = await session.get(StudyGroup, assignment.group_id)
    markup = notice_keyboard(
        [("Открыть задание", StudentCourseCallback(action="open", object_id=assignment.id).pack())],
        app=notifier.app_url(f"/tasks/a/{assignment.id}"),
    )
    text = (
        f"Новое задание в группе «{group.name}».\n{assignment.title}\n"
        f"Формат сдачи: {format_label(assignment.format)}"
    )
    students = await group_students(session, group_id=group.id)
    await notifier.send(bot, [Notice(student.telegram_id, text, markup) for student in students])


@_safe
async def notify_slot_opened(
    notifier: Notifier,
    bot: Bot,
    session: AsyncSession,
    *,
    slot_id: int,
    reopened: bool = False,
) -> None:
    context = await slot_context(session, slot_id=slot_id)
    if context is None:
        return
    slot = context.slot
    if (
        not slot.is_open
        or context.occupied >= slot.capacity
        or queue_service.slot_end_at(slot.date, slot.end_time) <= queue_service.now_moscow()
    ):
        return
    text = (
        ("Слот защиты снова открыт.\n" if reopened else "Открыт новый слот защиты.\n")
        + f"Группа: {context.group.name}\n"
        + f"Задание: {context.assignment.title}\n"
        + f"{_slot_line(slot)}\n"
        + f"Свободных мест: {slot.capacity - context.occupied}"
    )
    markup = notice_keyboard(
        [("Записаться", StudentQueueCallback(action="choose", object_id=slot.id).pack())],
        app=notifier.app_url(f"/defense/a/{context.assignment.id}"),
    )
    students = await approved_unbooked_students(session, assignment_id=context.assignment.id)
    await notifier.send(bot, [Notice(student.telegram_id, text, markup) for student in students])


@_safe
async def notify_student_joined(
    notifier: Notifier, bot: Bot, session: AsyncSession, *, group_id: int, student_id: int
) -> None:
    group = await session.get(StudyGroup, group_id)
    student = await session.get(User, student_id)
    if group is None or student is None:
        return
    teacher = await session.get(User, group.teacher_id)
    if teacher is None or teacher.role != Role.TEACHER:
        return
    if not (await teacher_preferences(session, teacher_id=teacher.id)).joins:
        return
    total = len(await group_students(session, group_id=group.id))
    await notifier.send(
        bot,
        [
            Notice(
                teacher.telegram_id,
                f"Новый студент в группе «{group.name}»: {_person(student)}.\n"
                f"Всего студентов: {total}.",
                notice_keyboard(
                    [
                        (
                            "Открыть группу",
                            TeacherGroupCallback(action="open", value=group.id).pack(),
                        )
                    ],
                    app=notifier.app_url(f"/groups/{group.id}"),
                    mute=NoticeCategory.JOINS,
                ),
            )
        ],
    )


@_safe
async def notify_queue_booking(
    notifier: Notifier,
    bot: Bot,
    session: AsyncSession,
    *,
    entry_id: int,
    switched: bool,
) -> None:
    entry = await session.get(QueueEntry, entry_id)
    if entry is None:
        return
    context = await submission_context(session, submission_id=entry.submission_id)
    slot = await slot_context(session, slot_id=entry.slot_id)
    if context is None or slot is None or context.teacher.role != Role.TEACHER:
        return
    if not (await teacher_preferences(session, teacher_id=context.teacher.id)).queue:
        return
    await notifier.send(
        bot,
        [
            Notice(
                context.teacher.telegram_id,
                f"{'Смена слота защиты' if switched else 'Запись на защиту'}: "
                f"{_person(context.student)}.\n"
                f"Группа: {context.group.name}\n"
                f"Задание: {context.assignment.title}\n"
                f"{_slot_line(slot.slot)}\n"
                f"Записано: {slot.occupied} из {slot.slot.capacity}.",
                notice_keyboard(
                    [
                        (
                            "Очередь слота",
                            TeacherQueueCallback(action="open", object_id=slot.slot.id).pack(),
                        )
                    ],
                    app=notifier.app_url(f"/slots/s/{slot.slot.id}"),
                    mute=NoticeCategory.QUEUE,
                ),
            )
        ],
    )


async def show_notification_settings(
    message: Message, session: AsyncSession, current_user: User
) -> None:
    if current_user.role != Role.TEACHER:
        await message.answer(
            "Студентам уведомления приходят всегда: новые задания, результат проверки "
            "и новые слоты защиты. Отключить их нельзя."
        )
        return
    preferences = await get_notification_preferences(session, actor_id=current_user.id)
    await message.answer(settings_text(preferences), reply_markup=settings_keyboard(preferences))


def create_router() -> Router:
    # The /notifications screen itself is opened from handlers.menu.
    router = Router(name="notifications")

    @router.callback_query(TeacherNotifyCallback.filter())
    async def switch(
        callback: CallbackQuery,
        callback_data: TeacherNotifyCallback,
        session: AsyncSession,
        current_user: User,
    ) -> None:
        categories = (
            list(NoticeCategory)
            if callback_data.category == "all"
            else [NoticeCategory(callback_data.category)]
        )
        preferences = await set_notification_preferences(
            session,
            actor_id=current_user.id,
            categories=categories,
            enabled=callback_data.enabled,
        )
        if callback_data.source == "notice":
            label = CATEGORY_LABELS[categories[0]]
            await callback.answer(
                f"Уведомления «{label}» выключены. Включить снова: «🔔 Уведомления».",
                show_alert=True,
            )
            return
        if isinstance(callback.message, Message):
            text, markup = settings_text(preferences), settings_keyboard(preferences)
            try:
                await callback.message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest:
                await callback.message.answer(text, reply_markup=markup)
        await callback.answer("Настройки уведомлений сохранены.")

    return router
