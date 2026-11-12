from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import DefenseSlot, User
from labdefender.enums import Role
from labdefender.handlers.coursework import answer_no_groups, format_label, send_text
from labdefender.keyboards.coursework import selection_keyboard
from labdefender.keyboards.defense import StudentQueueCallback, TeacherQueueCallback
from labdefender.services import queue as queue_service
from labdefender.services.assignments import get_assignment, list_assignments
from labdefender.services.groups import get_group, list_groups
from labdefender.services.pagination import PAGE_SIZE
from labdefender.services.queue import (
    get_slot_queue,
    get_student_booking,
    list_defense_slots,
    require_approved,
)


def slot_time_label(slot: DefenseSlot) -> str:
    return f"{slot.start_time:%H:%M}–{slot.end_time:%H:%M}"


def slot_title(slot: DefenseSlot) -> str:
    state = "закрыт" if not slot.is_open else ""
    return f"{slot.date:%d.%m.%Y} · {slot_time_label(slot)}" + (f" · {state}" if state else "")


async def show_queue_groups(
    message: Message, session: AsyncSession, user: User, *, new: bool = False, offset: int = 0
) -> None:
    page = await list_groups(session, actor_id=user.id, offset=offset)
    teacher = user.role == Role.TEACHER
    factory = TeacherQueueCallback if teacher else StudentQueueCallback
    action = ("assignments_new" if new else "assignments_view") if teacher else "assignments"
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
            previous=factory(
                action=page_action, object_id=0, offset=max(0, offset - PAGE_SIZE)
            ).pack()
            if offset
            else None,
            following=factory(action=page_action, object_id=0, offset=offset + PAGE_SIZE).pack()
            if page.has_next
            else None,
        ),
    )


async def show_queue_assignments(
    message: Message,
    session: AsyncSession,
    user: User,
    *,
    group_id: int,
    new: bool = False,
    offset: int = 0,
) -> None:
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    page = await list_assignments(session, actor_id=user.id, group_id=group_id, offset=offset)
    teacher = user.role == Role.TEACHER
    factory = TeacherQueueCallback if teacher else StudentQueueCallback
    action = ("new" if new else "days") if teacher else "status"
    page_action = ("assignments_new" if new else "assignments_view") if teacher else "assignments"
    await send_text(
        message,
        f"Группа «{group.name}».\n"
        + ("Выберите задание:" if page.items else "Заданий на этой странице нет."),
        reply_markup=selection_keyboard(
            [
                (
                    f"#{item.id} · {item.title} · {format_label(item.format)}",
                    factory(action=action, object_id=item.id).pack(),
                )
                for item in page.items
            ],
            previous=factory(
                action=page_action, object_id=group_id, offset=max(0, offset - PAGE_SIZE)
            ).pack()
            if offset
            else None,
            following=factory(
                action=page_action, object_id=group_id, offset=offset + PAGE_SIZE
            ).pack()
            if page.has_next
            else None,
        ),
    )


async def show_defense_slots(
    message: Message, session: AsyncSession, user: User, *, assignment_id: int, offset: int = 0
) -> None:
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    page = await list_defense_slots(
        session, actor_id=user.id, assignment_id=assignment_id, offset=offset
    )
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
                TeacherQueueCallback(action="new", object_id=assignment_id).pack(),
            )
        )
    else:
        buttons.append(
            ("Моя запись", StudentQueueCallback(action="status", object_id=assignment_id).pack())
        )
    await send_text(
        message,
        f"Задание: {assignment.title}\nФормат сдачи: {format_label(assignment.format)}\n"
        + ("Выберите слот (Москва):" if page.items else "Доступных слотов на этой странице нет."),
        reply_markup=selection_keyboard(
            buttons,
            previous=factory(
                action="days", object_id=assignment_id, offset=max(0, offset - PAGE_SIZE)
            ).pack()
            if offset
            else None,
            following=factory(
                action="days", object_id=assignment_id, offset=offset + PAGE_SIZE
            ).pack()
            if page.has_next
            else None,
        ),
    )


async def show_student_booking(
    message: Message, session: AsyncSession, user: User, *, assignment_id: int
) -> None:
    await require_approved(session, actor_id=user.id, assignment_id=assignment_id)
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    booking = await get_student_booking(session, actor_id=user.id, assignment_id=assignment_id)
    buttons = [
        (
            "Выбрать / сменить слот",
            StudentQueueCallback(action="days", object_id=assignment_id).pack(),
        )
    ]
    if booking is None:
        text = f"Задание: {assignment.title}\nДопуск получен. Вы ещё не выбрали слот защиты."
    else:
        text = (
            f"Задание: {assignment.title}\n"
            f"Слот: {booking.slot.date:%d.%m.%Y} · {slot_time_label(booking.slot)} (Москва)\n"
            f"Ваша позиция: {booking.position}\n"
            f"Записано: {booking.occupied} из {booking.slot.capacity}."
        )
        if (
            booking.slot.is_open
            and queue_service.slot_end_at(booking.slot.date, booking.slot.end_time)
            > queue_service.now_moscow()
        ):
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
    await send_text(message, text, reply_markup=selection_keyboard(buttons))


async def show_slot_queue(
    message: Message, session: AsyncSession, user: User, *, slot_id: int, offset: int = 0
) -> None:
    queue = await get_slot_queue(session, actor_id=user.id, slot_id=slot_id, offset=offset)
    assignment = await get_assignment(
        session, actor_id=user.id, assignment_id=queue.slot.assignment_id
    )
    lines = [
        f"Задание: {assignment.title}",
        f"Формат сдачи: {format_label(assignment.format)}",
        f"Слот: {queue.slot.date:%d.%m.%Y} · {slot_time_label(queue.slot)} (Москва)",
        f"Записано: {queue.occupied} из {queue.slot.capacity}."
        + (" Слот закрыт." if not queue.slot.is_open else ""),
    ]
    lines.extend(f"{member.position}. {member.student_name}" for member in queue.members.items)
    if not queue.members.items:
        lines.append("На этой странице записей нет.")
    buttons = [
        (
            "Обновить",
            TeacherQueueCallback(action="open", object_id=slot_id, offset=offset).pack(),
        ),
        ("Слоты защиты", TeacherQueueCallback(action="days", object_id=assignment.id).pack()),
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
            previous=TeacherQueueCallback(
                action="open", object_id=slot_id, offset=max(0, offset - PAGE_SIZE)
            ).pack()
            if offset
            else None,
            following=TeacherQueueCallback(
                action="open", object_id=slot_id, offset=offset + PAGE_SIZE
            ).pack()
            if queue.members.has_next
            else None,
        ),
    )
