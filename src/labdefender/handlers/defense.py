"""Chat screens of the defense queue. A slot belongs to a group (discipline); one place in it is
one approved work: the student, the assignment and the attempt."""

from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import DefenseSlot, User
from labdefender.enums import Role
from labdefender.handlers.coursework import answer_no_groups, send_text
from labdefender.keyboards.coursework import selection_keyboard
from labdefender.keyboards.defense import StudentQueueCallback, TeacherQueueCallback
from labdefender.services import queue as queue_service
from labdefender.services.assignments import get_assignment
from labdefender.services.groups import get_group, list_groups
from labdefender.services.pagination import PAGE_SIZE
from labdefender.services.queue import (
    get_slot_queue,
    get_student_booking,
    list_approved_unassigned,
    list_defense_slots,
    list_defense_works,
    require_approved,
)


def slot_time_label(slot: DefenseSlot) -> str:
    return f"{slot.start_time:%H:%M}–{slot.end_time:%H:%M}"


def slot_title(slot: DefenseSlot) -> str:
    state = "закрыт" if not slot.is_open else ""
    return f"{slot.date:%d.%m.%Y} · {slot_time_label(slot)}" + (f" · {state}" if state else "")


def slot_is_active(slot: DefenseSlot) -> bool:
    return (
        slot.is_open
        and queue_service.slot_end_at(slot.date, slot.end_time) > queue_service.now_moscow()
    )


def _pages(factory, action: str, object_id: int, offset: int, has_next: bool) -> dict:
    return {
        "previous": factory(
            action=action, object_id=object_id, offset=max(0, offset - PAGE_SIZE)
        ).pack()
        if offset
        else None,
        "following": factory(action=action, object_id=object_id, offset=offset + PAGE_SIZE).pack()
        if has_next
        else None,
    }


async def show_queue_groups(
    message: Message, session: AsyncSession, user: User, *, new: bool = False, offset: int = 0
) -> None:
    page = await list_groups(session, actor_id=user.id, offset=offset)
    teacher = user.role == Role.TEACHER
    factory = TeacherQueueCallback if teacher else StudentQueueCallback
    action = ("create" if new else "slots") if teacher else "assignments"
    page_action = ("groups_new" if new else "groups_view") if teacher else "groups"
    if not page.groups and not offset:
        await answer_no_groups(message, user)
        return
    await message.answer(
        "Выберите группу:",
        reply_markup=selection_keyboard(
            [
                (f"#{group.id} · {group.name}", factory(action=action, object_id=group.id).pack())
                for group in page.groups
            ],
            **_pages(factory, page_action, 0, offset, page.has_next),
        ),
    )


async def show_group_slots(
    message: Message, session: AsyncSession, user: User, *, group_id: int, offset: int = 0
) -> None:
    """Teacher: every slot of the group with its fill; students see only upcoming slots."""
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    page = await list_defense_slots(session, actor_id=user.id, group_id=group_id, offset=offset)
    teacher = user.role == Role.TEACHER
    factory = TeacherQueueCallback if teacher else StudentQueueCallback
    buttons = [
        (
            f"{slot_title(item.slot)} · {item.occupied}/{item.slot.capacity}"
            + (" · мест нет" if item.occupied >= item.slot.capacity else ""),
            factory(action="open" if teacher else "choose", object_id=item.slot.id).pack(),
        )
        for item in page.items
    ]
    if teacher:
        buttons.append(
            (
                "Создать слот защиты",
                TeacherQueueCallback(action="create", object_id=group_id).pack(),
            )
        )
        buttons.append(
            (
                "Допущенные без записи",
                TeacherQueueCallback(action="unbooked", object_id=group_id).pack(),
            )
        )
    else:
        buttons.append(
            ("Мои записи", StudentQueueCallback(action="assignments", object_id=group_id).pack())
        )
    await send_text(
        message,
        f"Группа «{group.name}». В слот записывается любая допущенная работа группы.\n"
        + ("Выберите слот (Москва):" if page.items else "Слотов на этой странице нет."),
        reply_markup=selection_keyboard(
            buttons, **_pages(factory, "slots", group_id, offset, page.has_next)
        ),
    )


async def show_unbooked(
    message: Message, session: AsyncSession, user: User, *, group_id: int, offset: int = 0
) -> None:
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    page = await list_approved_unassigned(
        session, actor_id=user.id, group_id=group_id, offset=offset
    )
    lines = [f"Группа «{group.name}». Допущенные работы без записи на защиту:"]
    lines.extend(
        f"• {item.student_name} · {item.assignment_title} · "
        f"попытка №{item.submission.attempt_number}"
        for item in page.items
    )
    if not page.items:
        lines.append("Таких работ нет." if not offset else "На этой странице записей нет.")
    await send_text(
        message,
        "\n".join(lines),
        reply_markup=selection_keyboard(
            [("Слоты защиты", TeacherQueueCallback(action="slots", object_id=group_id).pack())],
            **_pages(TeacherQueueCallback, "unbooked", group_id, offset, page.has_next),
        ),
    )


async def show_student_defense(
    message: Message, session: AsyncSession, user: User, *, group_id: int
) -> None:
    """Student: approved works of the group, where each one is booked, and the group's slots."""
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    works = await list_defense_works(session, actor_id=user.id, group_id=group_id)
    lines = [f"Группа «{group.name}». Защита:"]
    buttons = []
    for work in works:
        label = f"{work.assignment.title} · попытка №{work.submission.attempt_number}"
        booking = work.booking
        if booking is None:
            lines.append(f"• {label} — не записана")
        else:
            lines.append(
                f"• {label} — {booking.slot.date:%d.%m} · {slot_time_label(booking.slot)}, "
                f"позиция {booking.position} из {booking.occupied}"
            )
        buttons.append(
            (label, StudentQueueCallback(action="status", object_id=work.assignment.id).pack())
        )
    if not works:
        lines.append("Допущенных работ пока нет: запись откроется после допуска.")
    else:
        buttons.append(
            ("Слоты защиты", StudentQueueCallback(action="slots", object_id=group_id).pack())
        )
    await send_text(message, "\n".join(lines), reply_markup=selection_keyboard(buttons))


async def show_assignment_slots(
    message: Message, session: AsyncSession, user: User, *, assignment_id: int, offset: int = 0
) -> None:
    """Student: the group's upcoming slots to book (or move) this one approved work."""
    submission = await require_approved(session, actor_id=user.id, assignment_id=assignment_id)
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    page = await list_defense_slots(
        session, actor_id=user.id, group_id=assignment.group_id, offset=offset
    )
    buttons = [
        (
            f"{slot_title(item.slot)} · {item.occupied}/{item.slot.capacity}"
            + (" · мест нет" if item.occupied >= item.slot.capacity else ""),
            StudentQueueCallback(
                action="pick", object_id=assignment_id, slot_id=item.slot.id
            ).pack(),
        )
        for item in page.items
    ]
    buttons.append(
        ("Моя запись", StudentQueueCallback(action="status", object_id=assignment_id).pack())
    )
    await send_text(
        message,
        f"Задание: {assignment.title} · попытка №{submission.attempt_number}\n"
        + ("Выберите слот (Москва):" if page.items else "Доступных слотов на этой странице нет."),
        reply_markup=selection_keyboard(
            buttons, **_pages(StudentQueueCallback, "days", assignment_id, offset, page.has_next)
        ),
    )


async def show_student_booking(
    message: Message, session: AsyncSession, user: User, *, assignment_id: int
) -> None:
    submission = await require_approved(session, actor_id=user.id, assignment_id=assignment_id)
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    booking = await get_student_booking(session, actor_id=user.id, assignment_id=assignment_id)
    work = f"Задание: {assignment.title} · попытка №{submission.attempt_number}"
    buttons = [
        (
            "Выбрать / сменить слот",
            StudentQueueCallback(action="days", object_id=assignment_id).pack(),
        )
    ]
    if booking is None:
        text = f"{work}\nДопуск получен. Вы ещё не выбрали слот защиты."
    else:
        text = (
            f"{work}\n"
            f"Слот: {booking.slot.date:%d.%m.%Y} · {slot_time_label(booking.slot)} (Москва)\n"
            f"Ваша позиция: {booking.position}\n"
            f"Записано: {booking.occupied} из {booking.slot.capacity}."
        )
        if slot_is_active(booking.slot):
            buttons.insert(
                0,
                (
                    "В конец очереди",
                    StudentQueueCallback(action="move", object_id=assignment_id).pack(),
                ),
            )
    buttons.append(
        ("Обновить", StudentQueueCallback(action="status", object_id=assignment_id).pack())
    )
    buttons.append(
        (
            "Все мои записи",
            StudentQueueCallback(action="assignments", object_id=assignment.group_id).pack(),
        )
    )
    await send_text(message, text, reply_markup=selection_keyboard(buttons))


async def show_slot_queue(
    message: Message, session: AsyncSession, user: User, *, slot_id: int, offset: int = 0
) -> None:
    queue = await get_slot_queue(session, actor_id=user.id, slot_id=slot_id, offset=offset)
    group = await get_group(session, actor_id=user.id, group_id=queue.slot.group_id)
    lines = [
        f"Группа: {group.name}",
        f"Слот: {queue.slot.date:%d.%m.%Y} · {slot_time_label(queue.slot)} (Москва)",
        f"Записано: {queue.occupied} из {queue.slot.capacity}."
        + (" Слот закрыт." if not queue.slot.is_open else ""),
    ]
    lines.extend(
        f"{member.position}. {member.student_name} · {member.assignment_title} · "
        f"попытка №{member.attempt_number}"
        for member in queue.members.items
    )
    if not queue.members.items:
        lines.append("На этой странице записей нет.")
    buttons = [
        (
            "Обновить",
            TeacherQueueCallback(action="open", object_id=slot_id, offset=offset).pack(),
        ),
        ("Слоты защиты", TeacherQueueCallback(action="slots", object_id=group.id).pack()),
    ]
    if queue.slot.is_open:
        buttons.append(
            ("Закрыть слот", TeacherQueueCallback(action="close", object_id=slot_id).pack())
        )
    else:
        buttons.append(
            ("Открыть слот", TeacherQueueCallback(action="reopen", object_id=slot_id).pack())
        )
    if queue.occupied == 0:
        buttons.append(
            ("Отменить слот", TeacherQueueCallback(action="cancel", object_id=slot_id).pack())
        )
    await send_text(
        message,
        "\n".join(lines),
        reply_markup=selection_keyboard(
            buttons,
            **_pages(TeacherQueueCallback, "open", slot_id, offset, queue.members.has_next),
        ),
    )
