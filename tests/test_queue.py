import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Update
from sqlalchemy import func, select

from labdefender.app import create_dispatcher
from labdefender.db.models import DefenseSlot, Membership, QueueEntry, Submission
from labdefender.db.session import create_database, initialize_database
from labdefender.enums import AssignmentFormat, Role, SubmissionStatus
from labdefender.keyboards.coursework import TeacherCourseCallback
from labdefender.keyboards.defense import (
    StudentQueueCallback,
    StudentQueueDraftCallback,
    TeacherQueueCallback,
)
from labdefender.services.assignments import create_assignment
from labdefender.services.errors import (
    ApprovalRequired,
    DefenseSlotClosed,
    DefenseSlotExists,
    DefenseSlotFull,
    DefenseSlotNotEmpty,
    DefenseSlotOverlap,
    DomainError,
    InvalidAssignmentFormat,
    InvalidDefenseSlot,
    ObjectUnavailable,
    QueueChanged,
)
from labdefender.services.groups import create_group
from labdefender.services.queue import (
    cancel_defense_slot,
    close_defense_slot,
    create_defense_slot,
    enroll_slot,
    get_slot_queue,
    get_student_booking,
    list_approved_unassigned,
    list_defense_slots,
    move_to_end,
    reopen_defense_slot,
    today_moscow,
)
from labdefender.services.submissions import review_submission, submit_solution, text_solution
from labdefender.services.users import ensure_user

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 10)
NOW_MOSCOW = datetime(2026, 11, 10, 10, tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.fixture
async def context(database, monkeypatch):
    monkeypatch.setattr("labdefender.services.queue.now_moscow", lambda: NOW_MOSCOW)
    monkeypatch.setattr("labdefender.services.queue.utc_now", lambda: NOW)
    _, sessions = database
    ids, submissions = {}, {}
    async with sessions() as session:
        for telegram_id, role in [
            (101, Role.TEACHER),
            (202, Role.STUDENT),
            (203, Role.STUDENT),
            (204, Role.STUDENT),
            (303, Role.TEACHER),
            (404, Role.STUDENT),
        ]:
            user = await ensure_user(
                session, telegram_id=telegram_id, username=None, full_name=f"User{telegram_id}"
            )
            user.role = role
            await session.commit()
            ids[telegram_id] = user.id
    async with sessions() as session:
        group = await create_group(session, actor_id=ids[101], name="ИВТ-101")
    async with sessions() as session:
        session.add_all(
            [Membership(group_id=group.id, student_id=ids[number]) for number in (202, 203, 204)]
        )
        await session.commit()
    async with sessions() as session:
        assignment = await create_assignment(
            session,
            actor_id=ids[101],
            group_id=group.id,
            title="ЛР 1",
            description="Task",
            format=AssignmentFormat.ONLINE,
        )
    for index, number in enumerate((202, 203, 204)):
        async with sessions() as session:
            submission = await submit_solution(
                session,
                actor_id=ids[number],
                assignment_id=assignment.id,
                expected_previous_id=None,
                solution=text_solution("Work"),
            )
        async with sessions() as session:
            approved = await review_submission(
                session,
                actor_id=ids[101],
                submission_id=submission.id,
                decision=SubmissionStatus.APPROVED,
            )
            approved.approved_at = NOW - timedelta(hours=3 - index)
            await session.commit()
            submissions[number] = approved.id
    return sessions, ids, group, assignment, submissions


async def day(
    context,
    *,
    capacity=3,
    day_date=TODAY + timedelta(days=1),
    start_time=time(14, 0),
    end_time=time(16, 0),
):
    sessions, ids, _, assignment, _ = context
    async with sessions() as session:
        return await create_defense_slot(
            session,
            actor_id=ids[101],
            assignment_id=assignment.id,
            day_date=day_date,
            start_time=start_time,
            end_time=end_time,
            capacity=capacity,
        )


async def book(context, slot, *, student=202, previous=None):
    sessions, ids, _, assignment, _ = context
    async with sessions() as session:
        return await enroll_slot(
            session,
            actor_id=ids[student],
            assignment_id=assignment.id,
            slot_id=slot.id,
            expected_entry_id=previous.id if previous else None,
            expected_order_at=previous.order_at if previous else None,
        )


async def move(context, previous, *, student=202):
    sessions, ids, _, assignment, _ = context
    async with sessions() as session:
        return await move_to_end(
            session,
            actor_id=ids[student],
            assignment_id=assignment.id,
            expected_entry_id=previous.id,
            expected_order_at=previous.order_at,
        )


async def booking(context, *, student=202):
    sessions, ids, _, assignment, _ = context
    async with sessions() as session:
        return await get_student_booking(
            session, actor_id=ids[student], assignment_id=assignment.id
        )


async def test_initial_order_uses_approval_time_even_when_booking_later(context):
    slot = await day(context)
    await book(context, slot, student=203)
    first = await book(context, slot)
    assert (await booking(context)).position == 1
    assert (await booking(context, student=203)).position == 2
    sessions, ids, _, _, submissions = context
    async with sessions() as session:
        assert first.order_at == (await session.get(Submission, submissions[202])).approved_at
        queue = await get_slot_queue(session, actor_id=ids[101], slot_id=slot.id)
        assert [member.student_name for member in queue.members.items] == ["User202", "User203"]
        assert [member.position for member in queue.members.items] == [1, 2]


async def test_equal_approval_timestamps_use_entry_id_for_stable_order(context):
    sessions, _, _, _, submissions = context
    async with sessions() as session:
        for student in (202, 203):
            (await session.get(Submission, submissions[student])).approved_at = NOW
        await session.commit()
    slot = await day(context)
    await book(context, slot, student=203)
    await book(context, slot)
    assert (await booking(context, student=203)).position == 1
    assert (await booking(context)).position == 2


async def test_parallel_requests_for_last_seat_allow_only_one(context):
    slot = await day(context, capacity=1)
    results = await asyncio.gather(
        book(context, slot), book(context, slot, student=203), return_exceptions=True
    )
    assert sum(isinstance(result, QueueEntry) for result in results) == 1
    assert sum(isinstance(result, DefenseSlotFull) for result in results) == 1
    sessions, *_ = context
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueEntry)) == 1


async def test_parallel_duplicate_booking_uses_one_seat_and_preserves_order(context):
    slot = await day(context, capacity=1)
    first, repeated = await asyncio.gather(book(context, slot), book(context, slot))
    assert first.id == repeated.id
    assert first.order_at == repeated.order_at
    assert (await booking(context)).occupied == 1


async def test_failed_switch_preserves_previous_day_and_order(context):
    old_day = await day(context)
    full_day = await day(context, capacity=1, day_date=TODAY + timedelta(days=2))
    entry = await book(context, old_day)
    await book(context, full_day, student=203)
    with pytest.raises(DefenseSlotFull):
        await book(context, full_day, previous=entry)
    saved = await booking(context)
    assert saved.slot.id == old_day.id
    assert saved.entry.order_at == entry.order_at
    assert saved.occupied == 1


async def test_successful_switch_moves_existing_record_to_target_tail(context):
    old_day = await day(context)
    new_day = await day(context, day_date=TODAY + timedelta(days=2))
    entry = await book(context, old_day)
    await book(context, new_day, student=203)
    switched = await book(context, new_day, previous=entry)
    assert switched.id == entry.id
    assert (await booking(context)).position == 2
    assert (await booking(context, student=203)).occupied == 2
    sessions, ids, _, _, submissions = context
    async with sessions() as session:
        assert (await get_slot_queue(session, actor_id=ids[101], slot_id=old_day.id)).occupied == 0
        assert (await session.get(Submission, submissions[202])).approved_at == entry.order_at
        assert await session.scalar(select(func.count()).select_from(QueueEntry)) == 2


async def test_move_is_strictly_after_tail_and_repeated_old_version_is_rejected(context):
    sessions, _, _, _, submissions = context
    async with sessions() as session:
        (await session.get(Submission, submissions[203])).approved_at = NOW
        await session.commit()
    slot = await day(context)
    entry = await book(context, slot)
    await book(context, slot, student=203)
    moved = await move(context, entry)
    assert moved.order_at == NOW + timedelta(microseconds=1)
    assert (await booking(context)).position == 2
    with pytest.raises(QueueChanged):
        await move(context, entry)
    assert (await booking(context)).entry.order_at == moved.order_at


async def test_moving_last_entry_is_idempotent_without_timestamp_change(context):
    slot = await day(context)
    entry = await book(context, slot)
    assert (await move(context, entry)).order_at == entry.order_at
    assert (await move(context, entry)).order_at == entry.order_at


async def test_parallel_moves_do_not_apply_twice(context):
    slot = await day(context)
    entry = await book(context, slot)
    await book(context, slot, student=203)
    results = await asyncio.gather(
        move(context, entry), move(context, entry), return_exceptions=True
    )
    assert sum(isinstance(result, QueueEntry) for result in results) == 1
    assert sum(isinstance(result, QueueChanged) for result in results) == 1


async def test_stale_snapshot_cannot_switch_back_or_move_another_student(context):
    first_day = await day(context)
    second_day = await day(context, day_date=TODAY + timedelta(days=2))
    old = await book(context, first_day)
    current = await book(context, second_day, previous=old)
    with pytest.raises(QueueChanged):
        await book(context, first_day, previous=old)
    with pytest.raises(QueueChanged):
        await move(context, current, student=203)
    assert (await booking(context)).slot.id == second_day.id


@pytest.mark.parametrize(
    "capacity,day_date", [(0, TODAY), (-1, TODAY), (2**31, TODAY), (1, TODAY - timedelta(days=1))]
)
async def test_invalid_day_parameters_are_rejected(context, capacity, day_date):
    with pytest.raises(InvalidDefenseSlot):
        await day(context, capacity=capacity, day_date=day_date)


async def test_duplicate_day_is_idempotent_but_different_capacity_does_not_overwrite(context):
    first = await day(context)
    assert (await day(context)).id == first.id
    with pytest.raises(DefenseSlotExists):
        await day(context, capacity=5)


async def test_no_approval_and_wrong_assignment_cannot_book(context):
    sessions, ids, group, assignment, _ = context
    async with sessions() as session:
        unapproved = await create_assignment(
            session,
            actor_id=ids[101],
            group_id=group.id,
            title="Pending task",
            description="Task",
            format=AssignmentFormat.ONLINE,
        )
    async with sessions() as session:
        pending = await submit_solution(
            session,
            actor_id=ids[202],
            assignment_id=unapproved.id,
            expected_previous_id=None,
            solution=text_solution("Work"),
        )
    async with sessions() as session:
        target = await create_defense_slot(
            session,
            actor_id=ids[101],
            assignment_id=unapproved.id,
            day_date=TODAY,
            start_time=time(14, 0),
            end_time=time(16, 0),
            capacity=1,
        )
    async with sessions() as session:
        with pytest.raises(ApprovalRequired):
            await enroll_slot(
                session,
                actor_id=ids[202],
                assignment_id=unapproved.id,
                slot_id=target.id,
                expected_entry_id=None,
                expected_order_at=None,
            )
    async with sessions() as session:
        with pytest.raises(ObjectUnavailable):
            await enroll_slot(
                session,
                actor_id=ids[202],
                assignment_id=assignment.id,
                slot_id=target.id,
                expected_entry_id=None,
                expected_order_at=None,
            )
    async with sessions() as session:
        assert (await session.get(Submission, pending.id)).status == SubmissionStatus.PENDING
        assert await session.scalar(select(func.count()).select_from(QueueEntry)) == 0


async def test_roles_and_foreign_groups_cannot_read_or_mutate_queue(context):
    sessions, ids, _, assignment, _ = context
    slot = await day(context)
    for actor in (202, 303, 404):
        async with sessions() as session:
            with pytest.raises(DomainError):
                await get_slot_queue(session, actor_id=ids[actor], slot_id=slot.id)
    for actor in (101, 303, 404):
        async with sessions() as session:
            with pytest.raises(DomainError):
                await enroll_slot(
                    session,
                    actor_id=ids[actor],
                    assignment_id=assignment.id,
                    slot_id=slot.id,
                    expected_entry_id=None,
                    expected_order_at=None,
                )
    async with sessions() as session:
        with pytest.raises(DomainError):
            await create_defense_slot(
                session,
                actor_id=ids[303],
                assignment_id=assignment.id,
                day_date=TODAY,
                start_time=time(14, 0),
                end_time=time(16, 0),
                capacity=1,
            )


async def test_past_days_are_hidden_from_students_and_closed_for_writes(context, monkeypatch):
    slot = await day(context)
    entry = await book(context, slot)
    monkeypatch.setattr(
        "labdefender.services.queue.now_moscow", lambda: NOW_MOSCOW + timedelta(days=2)
    )
    sessions, ids, _, assignment, _ = context
    async with sessions() as session:
        assert not (
            await list_defense_slots(session, actor_id=ids[202], assignment_id=assignment.id)
        ).items
        assert (
            await list_defense_slots(session, actor_id=ids[101], assignment_id=assignment.id)
        ).items[0].slot.id == slot.id
    with pytest.raises(DefenseSlotClosed):
        await book(context, slot, student=203)
    with pytest.raises(DefenseSlotClosed):
        await move(context, entry)


async def test_today_is_available_and_uses_moscow_day_boundary(context):
    slot = await day(context, day_date=TODAY)
    await book(context, slot)
    assert today_moscow(datetime(2026, 11, 10, 21, 30, tzinfo=UTC)) == date(2026, 11, 11)
    assert today_moscow(datetime(2026, 11, 10, 20, 59, tzinfo=UTC)) == TODAY


async def test_queue_survives_database_reopen(context, database):
    slot = await day(context)
    await book(context, slot)
    await book(context, slot, student=203)
    original = await booking(context)
    sessions, ids, _, assignment, _ = context
    old_engine, _ = database
    engine, reopened = create_database(str(old_engine.url))
    try:
        await initialize_database(engine)
        async with reopened() as session:
            stored = await get_student_booking(
                session, actor_id=ids[202], assignment_id=assignment.id
            )
            assert stored.position == original.position
            assert stored.entry.id == original.entry.id
            assert stored.entry.order_at == original.entry.order_at
    finally:
        await engine.dispose()


async def test_assignment_format_is_persisted_and_validated(context):
    sessions, ids, group, _, _ = context
    async with sessions() as session:
        created = await create_assignment(
            session,
            actor_id=ids[101],
            group_id=group.id,
            title="Очная работа",
            description="Task",
            format=AssignmentFormat.IN_PERSON,
        )
        assert created.format == AssignmentFormat.IN_PERSON
    async with sessions() as session:
        with pytest.raises(InvalidAssignmentFormat):
            await create_assignment(
                session,
                actor_id=ids[101],
                group_id=group.id,
                title="Плохой формат",
                description="Task",
                format="bad",
            )


async def test_multiple_slots_per_day_and_overlap_validation(context):
    first = await day(context, capacity=2)
    second = await day(
        context,
        capacity=2,
        start_time=time(16, 0),
        end_time=time(18, 0),
    )
    assert first.id != second.id
    with pytest.raises(DefenseSlotOverlap):
        await day(
            context,
            capacity=2,
            start_time=time(15, 0),
            end_time=time(17, 0),
        )


async def test_close_reopen_and_cancel_empty_slot(context):
    sessions, ids, _, assignment, _ = context
    slot = await day(context, capacity=2)
    async with sessions() as session:
        closed = await close_defense_slot(session, actor_id=ids[101], slot_id=slot.id)
        assert not closed.is_open
    with pytest.raises(DefenseSlotClosed):
        await book(context, slot, student=202)
    async with sessions() as session:
        reopened = await reopen_defense_slot(session, actor_id=ids[101], slot_id=slot.id)
        assert reopened.slot.is_open and reopened.reopened
    async with sessions() as session:
        repeated = await reopen_defense_slot(session, actor_id=ids[101], slot_id=slot.id)
        assert repeated.slot.is_open and not repeated.reopened
    await book(context, slot, student=202)
    async with sessions() as session:
        with pytest.raises(DefenseSlotNotEmpty):
            await cancel_defense_slot(session, actor_id=ids[101], slot_id=slot.id)
    empty = await day(context, capacity=1, day_date=TODAY + timedelta(days=2))
    async with sessions() as session:
        removed = await cancel_defense_slot(session, actor_id=ids[101], slot_id=empty.id)
        assert removed.id == empty.id
    with pytest.raises(ObjectUnavailable):
        await book(context, empty, student=203)


async def test_teacher_sees_approved_students_without_slot(context):
    sessions, ids, _, assignment, _ = context
    async with sessions() as session:
        page = await list_approved_unassigned(
            session, actor_id=ids[101], assignment_id=assignment.id
        )
        assert {item.student_name for item in page.items} == {"User202", "User203", "User204"}
    slot = await day(context, capacity=2)
    await book(context, slot, student=202)
    async with sessions() as session:
        page = await list_approved_unassigned(
            session, actor_id=ids[101], assignment_id=assignment.id
        )
        names = {item.student_name for item in page.items}
        assert "User202" not in names
        assert {"User203", "User204"} <= names
    async with sessions() as session:
        with pytest.raises(DomainError):
            await list_approved_unassigned(session, actor_id=ids[202], assignment_id=assignment.id)


def message_update(text, *, user_id=101):
    message = {
        "message_id": 10,
        "date": datetime.now(UTC),
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}"},
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.model_validate({"update_id": 1, "message": message})


def callback_update(data, *, user_id=101):
    message = message_update("Menu", user_id=user_id).message.model_dump(by_alias=True)
    message["from"] = {"id": 123456, "is_bot": True, "first_name": "Bot"}
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "queue-callback",
                "chat_instance": "test-chat",
                "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}"},
                "message": message,
            },
        }
    )


def messages(bot):
    return [call for call in bot.session.calls if isinstance(call, SendMessage)]


def first_button(bot):
    return messages(bot)[-1].reply_markup.inline_keyboard[0][0].callback_data


async def press(dispatcher, bot, callback, *, user=101):
    await dispatcher.feed_update(
        bot,
        callback_update(callback.pack() if hasattr(callback, "pack") else callback, user_id=user),
    )


async def test_teacher_opens_and_refreshes_unassigned(context, bot):
    sessions, _, _, assignment, _ = context
    dispatcher = create_dispatcher(sessions)
    await press(dispatcher, bot, TeacherCourseCallback(action="attempts", object_id=assignment.id))
    buttons = messages(bot)[-1].reply_markup.inline_keyboard
    entry = next(b for row in buttons for b in row if b.text == "Допущены, но не записались")
    await press(dispatcher, bot, entry.callback_data)
    assert "User202" in messages(bot)[-1].text
    refresh = first_button(bot)
    slot = await day(context, capacity=3)
    for student in (202, 203, 204):
        await book(context, slot, student=student)
    await press(dispatcher, bot, refresh)
    assert "Нет допущенных студентов без записи." in messages(bot)[-1].text


async def prepare_booking(dispatcher, bot, slot, *, user=202):
    await press(
        dispatcher, bot, StudentQueueCallback(action="choose", object_id=slot.id), user=user
    )
    return first_button(bot)


async def test_full_bot_slot_creation_booking_position_move_and_teacher_queue(context, bot):
    sessions, ids, _, assignment, _ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/new_slot"))
    await press(dispatcher, bot, first_button(bot))
    await press(dispatcher, bot, first_button(bot))
    await dispatcher.feed_update(bot, message_update("not-a-date"))
    assert "YYYY-MM-DD" in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("2026-11-11"))
    await dispatcher.feed_update(bot, message_update("not-a-time"))
    assert "HH:MM" in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("14:00"))
    await dispatcher.feed_update(bot, message_update("13:00"))
    assert "конец позже начала" in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("16:00"))
    await dispatcher.feed_update(bot, message_update("0"))
    assert "положительный лимит" in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("2"))
    confirmation = first_button(bot)
    await press(dispatcher, bot, confirmation)
    await press(dispatcher, bot, confirmation)
    assert "устарела" in bot.session.calls[-1].text
    async with sessions() as session:
        slot = await session.scalar(select(DefenseSlot))
    await dispatcher.feed_update(bot, message_update("/queue", user_id=202))
    await press(dispatcher, bot, first_button(bot), user=202)
    await press(dispatcher, bot, first_button(bot), user=202)
    assert "ещё не выбрали слот" in messages(bot)[-1].text
    await press(dispatcher, bot, first_button(bot), user=202)
    confirmation = await prepare_booking(dispatcher, bot, slot)
    await press(dispatcher, bot, confirmation, user=202)
    assert "Ваша позиция: 1" in messages(bot)[-1].text
    await press(dispatcher, bot, confirmation, user=202)
    assert "устарела" in bot.session.calls[-1].text
    confirmation = await prepare_booking(dispatcher, bot, slot, user=203)
    await press(dispatcher, bot, confirmation, user=203)
    await press(
        dispatcher, bot, StudentQueueCallback(action="move", object_id=assignment.id), user=202
    )
    await press(dispatcher, bot, first_button(bot), user=202)
    assert "Ваша позиция: 2" in messages(bot)[-1].text
    assert "User203" not in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("/queue"))
    await press(dispatcher, bot, first_button(bot))
    await press(dispatcher, bot, first_button(bot))
    await press(dispatcher, bot, first_button(bot))
    assert "Записано: 2 из 2" in messages(bot)[-1].text
    assert "1. User203" in messages(bot)[-1].text
    assert "2. User202" in messages(bot)[-1].text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueEntry)) == 2


async def test_switch_can_fail_at_confirmation_without_losing_original_booking(context, bot):
    original_day = await day(context)
    target = await day(context, capacity=1, day_date=TODAY + timedelta(days=2))
    await book(context, original_day)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_booking(dispatcher, bot, target)
    await book(context, target, student=203)
    await press(dispatcher, bot, confirmation, user=202)
    assert "мест нет" in bot.session.calls[-1].text
    assert (await booking(context)).slot.id == original_day.id


@pytest.mark.parametrize("cancel", ["/cancel", "button", "restart"])
async def test_cancelled_or_lost_queue_confirmation_does_not_book(context, bot, cancel):
    target = await day(context)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_booking(dispatcher, bot, target)
    if cancel == "restart":
        dispatcher = create_dispatcher(sessions)
    elif cancel == "button":
        data = StudentQueueDraftCallback.unpack(confirmation)
        data.action = "cancel"
        await press(dispatcher, bot, data, user=202)
    else:
        await dispatcher.feed_update(bot, message_update(cancel, user_id=202))
    await press(dispatcher, bot, confirmation, user=202)
    assert "устарела" in bot.session.calls[-1].text
    assert await booking(context) is None


async def test_queue_callbacks_do_not_expose_other_teacher_or_student_names(context, bot):
    target = await day(context)
    await book(context, target)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    for user in (202, 303):
        await press(
            dispatcher, bot, TeacherQueueCallback(action="open", object_id=target.id), user=user
        )
        assert isinstance(bot.session.calls[-1], AnswerCallbackQuery)
    assert not messages(bot)


async def test_delivery_failure_after_queue_commit_is_safe_to_retry(context, bot, monkeypatch):
    target = await day(context)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_booking(dispatcher, bot, target)
    original = bot.session.make_request

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, SendMessage) and method.text == "Запись в очереди сохранена.":
            raise TelegramNetworkError(method=method, message="Example delivery failure")
        return await original(bot, method, timeout)

    with monkeypatch.context() as patch:
        patch.setattr(bot.session, "make_request", request)
        with pytest.raises(TelegramNetworkError):
            await press(dispatcher, bot, confirmation, user=202)
    await press(dispatcher, bot, confirmation, user=202)
    assert (await booking(context)).occupied == 1


async def test_past_booking_can_be_viewed_and_rescheduled_but_has_no_move_button(
    context, bot, monkeypatch
):
    previous_day = await day(context)
    await book(context, previous_day)
    future_day = await day(context, day_date=TODAY + timedelta(days=3))
    monkeypatch.setattr(
        "labdefender.services.queue.now_moscow", lambda: NOW_MOSCOW + timedelta(days=2)
    )
    sessions, _, _, assignment, _ = context
    dispatcher = create_dispatcher(sessions)
    await press(
        dispatcher, bot, StudentQueueCallback(action="status", object_id=assignment.id), user=202
    )
    assert not any(
        button.text == "В конец очереди"
        for row in messages(bot)[-1].reply_markup.inline_keyboard
        for button in row
    )
    confirmation = await prepare_booking(dispatcher, bot, future_day)
    await press(dispatcher, bot, confirmation, user=202)
    assert (await booking(context)).slot.id == future_day.id
