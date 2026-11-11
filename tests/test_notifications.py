from datetime import timedelta

import pytest
import test_queue
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import select
from test_queue import TODAY, book, day, message_update, prepare_booking, press

from labdefender.app import create_dispatcher
from labdefender.db.models import DefenseSlot, NotificationSetting, StudyGroup
from labdefender.enums import AssignmentFormat
from labdefender.handlers.notifications import Notice, Notifier
from labdefender.keyboards.coursework import (
    AssignmentFormatCallback,
    StudentCourseCallback,
    TeacherCourseCallback,
)
from labdefender.keyboards.defense import StudentQueueCallback, TeacherQueueCallback
from labdefender.keyboards.notifications import TeacherNotifyCallback
from labdefender.services.assignments import create_assignment
from labdefender.services.submissions import submit_solution, text_solution

# Reuse the queue fixture: teacher 101 owns a group with approved students 202, 203 and 204.
context = test_queue.context
TEACHER, STUDENTS = 101, (202, 203, 204)


def sent(bot, chat_id):
    return [
        call
        for call in bot.session.calls
        if isinstance(call, SendMessage) and int(call.chat_id) == chat_id
    ]


def labels(message):
    return [button.text for row in message.reply_markup.inline_keyboard for button in row]


def button(message, text):
    return next(
        item.callback_data
        for row in message.reply_markup.inline_keyboard
        for item in row
        if item.text == text
    )


async def new_assignment(context, title="ЛР 2"):
    sessions, ids, group, *_ = context
    async with sessions() as session:
        return await create_assignment(
            session,
            actor_id=ids[TEACHER],
            group_id=group.id,
            title=title,
            description="Task",
            format=AssignmentFormat.IN_PERSON,
        )


async def pending(context, assignment, student=202):
    sessions, ids, *_ = context
    async with sessions() as session:
        return await submit_solution(
            session,
            actor_id=ids[student],
            assignment_id=assignment.id,
            expected_previous_id=None,
            solution=text_solution("Work"),
        )


async def send_solution(dispatcher, bot, assignment, *, student):
    await press(
        dispatcher,
        bot,
        StudentCourseCallback(action="submit", object_id=assignment.id),
        user=student,
    )
    await dispatcher.feed_update(bot, message_update("Решение", user_id=student))
    await press(
        dispatcher,
        bot,
        sent(bot, student)[-1].reply_markup.inline_keyboard[0][0].callback_data,
        user=student,
    )


async def test_teacher_learns_about_new_member_once(context, bot):
    sessions, *_ = context
    async with sessions() as session:
        group = await session.scalar(select(StudyGroup))
    dispatcher = create_dispatcher(sessions)
    for _ in range(2):
        await dispatcher.feed_update(
            bot, message_update(f"/start join_{group.invite_token}", user_id=405)
        )
    [notice] = sent(bot, TEACHER)
    assert notice.text == "Новый студент в группе «ИВТ-101»: User405.\nВсего студентов: 4."
    assert labels(notice) == ["Открыть группу", "🔕 Не присылать такие"]
    assert "Вы уже в группе" in sent(bot, 405)[-1].text


async def test_submission_notice_and_mute_from_notice_and_settings(context, bot):
    assignment = await new_assignment(context)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await send_solution(dispatcher, bot, assignment, student=202)
    [notice] = sent(bot, TEACHER)
    assert notice.text == (
        "Новое решение на проверку.\nГруппа: ИВТ-101\nЗадание: ЛР 2\n"
        "Студент: User202\nПопытка №1 · текст"
    )
    assert "отправлена на проверку" in sent(bot, 202)[-1].text
    opened = TeacherCourseCallback.unpack(button(notice, "Открыть попытку"))
    assert opened.action == "open"

    await press(dispatcher, bot, button(notice, "🔕 Не присылать такие"))
    answer = bot.session.calls[-1]
    assert isinstance(answer, AnswerCallbackQuery) and answer.show_alert
    assert "«новые решения на проверку» выключены" in answer.text
    await send_solution(dispatcher, bot, assignment, student=203)
    assert len(sent(bot, TEACHER)) == 1

    await dispatcher.feed_update(bot, message_update("/notifications"))
    screen = sent(bot, TEACHER)[-1]
    assert "🔕 Новые решения на проверку" in screen.text
    assert "✅ Вступление студентов в группы" in screen.text
    assert labels(screen)[0] == "Включить: новые решения на проверку"
    await press(dispatcher, bot, button(screen, "Включить все"))
    edited = bot.session.calls[-2]
    assert isinstance(edited, EditMessageText) and "✅ Новые решения" in edited.text
    await send_solution(dispatcher, bot, assignment, student=204)
    assert "User204" in sent(bot, TEACHER)[-1].text


async def test_settings_buttons_toggle_each_category_and_all(context, bot):
    sessions, ids, *_ = context
    dispatcher = create_dispatcher(sessions)
    await press(dispatcher, bot, TeacherNotifyCallback(category="queue", enabled=False))
    await press(dispatcher, bot, TeacherNotifyCallback(category="joins", enabled=False))
    async with sessions() as session:
        row = await session.get(NotificationSetting, ids[TEACHER])
        assert (row.submissions, row.joins, row.queue) == (True, False, False)
    await press(dispatcher, bot, TeacherNotifyCallback(category="all", enabled=False))
    async with sessions() as session:
        row = await session.get(NotificationSetting, ids[TEACHER])
        assert (row.submissions, row.joins, row.queue) == (False, False, False)


async def test_students_cannot_mute_and_learn_notifications_are_always_on(context, bot):
    sessions, ids, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/notifications", user_id=202))
    assert "приходят всегда" in sent(bot, 202)[-1].text
    await press(dispatcher, bot, TeacherNotifyCallback(category="all", enabled=False), user=202)
    assert bot.session.calls[-1].show_alert
    async with sessions() as session:
        assert await session.get(NotificationSetting, ids[202]) is None


async def test_student_learns_review_result_with_full_comment(context, bot):
    assignment = await new_assignment(context)
    first = await pending(context, assignment)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await press(dispatcher, bot, TeacherCourseCallback(action="return", object_id=first.id))
    comment = "Исправьте. " * 360
    await dispatcher.feed_update(bot, message_update(comment))
    notices = sent(bot, 202)
    assert len(notices) == 2 and notices[0].reply_markup is None
    assert notices[0].text.startswith("Работа возвращена на доработку.\nГруппа: ИВТ-101")
    assert "Комментарий преподавателя:\n" in notices[0].text
    assert "".join(notice.text for notice in notices).endswith(comment.strip())
    assert labels(notices[-1]) == ["Открыть задание"]
    assert "Студенту отправлено уведомление" in sent(bot, TEACHER)[-1].text

    async with sessions() as session:
        second = await submit_solution(
            session,
            actor_id=context[1][202],
            assignment_id=assignment.id,
            expected_previous_id=first.id,
            solution=text_solution("Fixed"),
        )
    await press(dispatcher, bot, TeacherCourseCallback(action="approve", object_id=second.id))
    approved = sent(bot, 202)[-1]
    assert approved.text == (
        "Работа допущена.\nГруппа: ИВТ-101\nЗадание: ЛР 2\nПопытка №2\n"
        "Теперь можно выбрать слот защиты."
    )
    days = StudentQueueCallback.unpack(button(approved, "Выбрать слот защиты"))
    assert (days.action, days.object_id) == ("days", assignment.id)
    await press(dispatcher, bot, TeacherCourseCallback(action="approve", object_id=second.id))
    assert len(sent(bot, 202)) == 3  # A stale repeated approval does not notify again.


async def test_group_members_learn_about_new_assignment(context, bot):
    sessions, _, group, *_ = context
    dispatcher = create_dispatcher(sessions)
    await press(dispatcher, bot, TeacherCourseCallback(action="publish", object_id=group.id))
    await press(dispatcher, bot, AssignmentFormatCallback(action="in_person"))
    await dispatcher.feed_update(bot, message_update("ЛР 3"))
    await dispatcher.feed_update(bot, message_update("Условие"))
    await press(
        dispatcher, bot, sent(bot, TEACHER)[-1].reply_markup.inline_keyboard[0][0].callback_data
    )
    for student in STUDENTS:
        [notice] = sent(bot, student)
        assert notice.text == "Новое задание в группе «ИВТ-101».\nЛР 3\nФормат сдачи: очная"
        assert StudentCourseCallback.unpack(button(notice, "Открыть задание")).action == "open"
    assert "опубликовано" in sent(bot, TEACHER)[-1].text


async def test_new_and_reopened_slots_reach_only_approved_students_without_booking(context, bot):
    sessions, _, _, assignment, _ = context
    await book(context, await day(context), student=202)
    dispatcher = create_dispatcher(sessions)
    await press(dispatcher, bot, TeacherQueueCallback(action="new", object_id=assignment.id))
    for text in ("2026-11-12", "10:00", "12:00", "2"):
        await dispatcher.feed_update(bot, message_update(text))
    await press(dispatcher, bot, button(sent(bot, TEACHER)[-1], "Подтвердить"))
    assert len(sent(bot, 202)) == 0
    for student in (203, 204):
        [notice] = sent(bot, student)
        assert notice.text == (
            "Открыт новый слот защиты.\nГруппа: ИВТ-101\nЗадание: ЛР 1\n"
            "Слот: 12.11.2026 (чт) · 10:00–12:00 (Москва)\nСвободных мест: 2"
        )
        assert StudentQueueCallback.unpack(button(notice, "Записаться")).action == "choose"

    async with sessions() as session:
        slot = await session.scalar(
            select(DefenseSlot).where(DefenseSlot.date == TODAY + timedelta(days=2))
        )
    await press(dispatcher, bot, TeacherQueueCallback(action="close", object_id=slot.id))
    await press(dispatcher, bot, TeacherQueueCallback(action="reopen", object_id=slot.id))
    await press(dispatcher, bot, TeacherQueueCallback(action="reopen", object_id=slot.id))
    for student in (203, 204):
        assert len(sent(bot, student)) == 2
        assert sent(bot, student)[-1].text.startswith("Слот защиты снова открыт.")


async def test_teacher_learns_about_booking_and_switch_but_not_move(context, bot):
    first, second = await day(context), await day(context, day_date=TODAY + timedelta(days=2))
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await press(dispatcher, bot, await prepare_booking(dispatcher, bot, first), user=202)
    [notice] = sent(bot, TEACHER)
    assert notice.text == (
        "Запись на защиту: User202.\nГруппа: ИВТ-101\nЗадание: ЛР 1\n"
        "Слот: 11.11.2026 (ср) · 14:00–16:00 (Москва)\nЗаписано: 1 из 3."
    )
    assert TeacherQueueCallback.unpack(button(notice, "Очередь слота")).object_id == first.id
    assert "Ваша позиция: 1" in sent(bot, 202)[-1].text
    await press(dispatcher, bot, await prepare_booking(dispatcher, bot, first, user=203), user=203)
    await press(
        dispatcher,
        bot,
        StudentQueueCallback(action="move", object_id=first.assignment_id),
        user=202,
    )
    await press(
        dispatcher,
        bot,
        sent(bot, 202)[-1].reply_markup.inline_keyboard[0][0].callback_data,
        user=202,
    )
    assert len(sent(bot, TEACHER)) == 2
    await press(dispatcher, bot, await prepare_booking(dispatcher, bot, second), user=202)
    assert sent(bot, TEACHER)[-1].text.startswith("Смена слота защиты: User202.")
    await press(dispatcher, bot, button(sent(bot, TEACHER)[-1], "🔕 Не присылать такие"))
    await press(dispatcher, bot, await prepare_booking(dispatcher, bot, second, user=204), user=204)
    assert len(sent(bot, TEACHER)) == 3


async def test_blocked_recipient_does_not_stop_others_or_the_action(context, bot, monkeypatch):
    sessions, _, group, *_ = context
    original = bot.session.make_request

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, SendMessage) and int(method.chat_id) == 203:
            raise TelegramForbiddenError(method=method, message="bot was blocked by the user")
        return await original(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", request)
    dispatcher = create_dispatcher(sessions)
    await press(dispatcher, bot, TeacherCourseCallback(action="publish", object_id=group.id))
    await press(dispatcher, bot, AssignmentFormatCallback(action="online"))
    await dispatcher.feed_update(bot, message_update("ЛР 4"))
    await dispatcher.feed_update(bot, message_update("Условие"))
    await press(
        dispatcher, bot, sent(bot, TEACHER)[-1].reply_markup.inline_keyboard[0][0].callback_data
    )
    assert sent(bot, 202) and sent(bot, 204)
    assert "опубликовано" in sent(bot, TEACHER)[-1].text


async def test_background_notifier_retries_flood_limit_and_finishes_on_close(bot, monkeypatch):
    original = bot.session.make_request
    failures = []

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, SendMessage) and not failures:
            failures.append(method)
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=0)
        return await original(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", request)
    notifier = Notifier(background=True)
    await notifier.send(bot, [Notice(1, "first"), Notice(2, "second")])
    assert not sent(bot, 1)
    await notifier.close()
    assert [call.text for call in bot.session.calls] == ["first", "second"]


async def test_background_dispatcher_does_not_wait_for_delivery(context, bot):
    sessions, *_ = context
    notifier = Notifier(background=True)
    dispatcher = create_dispatcher(sessions, notifier=notifier)
    assignment = await new_assignment(context)
    await send_solution(dispatcher, bot, assignment, student=202)
    await notifier.wait_idle()
    assert "Новое решение на проверку." in sent(bot, TEACHER)[-1].text


@pytest.mark.parametrize("blocked", [False, True])
async def test_notification_errors_never_fail_the_action(context, bot, monkeypatch, blocked):
    async def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    if blocked:
        monkeypatch.setattr("labdefender.handlers.notifications.submission_context", broken)
    assignment = await new_assignment(context)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await send_solution(dispatcher, bot, assignment, student=202)
    assert "отправлена на проверку" in sent(bot, 202)[-1].text
    assert bool(sent(bot, TEACHER)) is not blocked
