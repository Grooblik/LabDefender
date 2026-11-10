from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.handlers.coursework import send_text
from labdefender.keyboards.coursework import selection_keyboard
from labdefender.keyboards.defense import StudentQueueCallback, TeacherQueueCallback
from labdefender.services import queue as queue_service
from labdefender.services.assignments import get_assignment, list_assignments
from labdefender.services.groups import get_group, list_groups
from labdefender.services.pagination import PAGE_SIZE
from labdefender.services.queue import (
    get_day_queue,
    get_student_booking,
    list_defense_days,
    require_approved,
)


async def show_queue_groups(
    message: Message, session: AsyncSession, user: User, *, new: bool = False, offset: int = 0
) -> None:
    page = await list_groups(session, actor_id=user.id, offset=offset)
    teacher = user.role == Role.TEACHER
    factory = TeacherQueueCallback if teacher else StudentQueueCallback
    action = ("assignments_new" if new else "assignments_view") if teacher else "assignments"
    page_action = ("groups_new" if new else "groups_view") if teacher else "groups"
    if not page.groups and not offset:
        await message.answer("Нет доступных групп. Откройте /my_groups.")
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
                (f"#{item.id} · {item.title}", factory(action=action, object_id=item.id).pack())
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


async def show_defense_days(
    message: Message, session: AsyncSession, user: User, *, assignment_id: int, offset: int = 0
) -> None:
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    page = await list_defense_days(
        session, actor_id=user.id, assignment_id=assignment_id, offset=offset
    )
    teacher = user.role == Role.TEACHER
    factory = TeacherQueueCallback if teacher else StudentQueueCallback
    buttons = [
        (
            f"{item.day.date:%d.%m.%Y} · {item.occupied}/{item.day.capacity}"
            + (" · мест нет" if item.occupied >= item.day.capacity else ""),
            factory(action="open" if teacher else "choose", object_id=item.day.id).pack(),
        )
        for item in page.items
    ]
    if teacher:
        buttons.append(
            (
                "Создать день защиты",
                TeacherQueueCallback(action="new", object_id=assignment_id).pack(),
            )
        )
    else:
        buttons.append(
            ("Моя запись", StudentQueueCallback(action="status", object_id=assignment_id).pack())
        )
    await send_text(
        message,
        f"Задание: {assignment.title}\n"
        + ("Выберите дату (Москва):" if page.items else "Доступных дат на этой странице нет."),
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
            "Выбрать / сменить день",
            StudentQueueCallback(action="days", object_id=assignment_id).pack(),
        )
    ]
    if booking is None:
        text = f"Задание: {assignment.title}\nДопуск получен. Вы ещё не выбрали день защиты."
    else:
        text = (
            f"Задание: {assignment.title}\nДата: {booking.day.date:%d.%m.%Y} (Москва)\n"
            f"Ваша позиция: {booking.position}\n"
            f"Записано: {booking.occupied} из {booking.day.capacity}."
        )
        if booking.day.date >= queue_service.today_moscow():
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


async def show_day_queue(
    message: Message, session: AsyncSession, user: User, *, defense_day_id: int, offset: int = 0
) -> None:
    queue = await get_day_queue(
        session, actor_id=user.id, defense_day_id=defense_day_id, offset=offset
    )
    assignment = await get_assignment(
        session, actor_id=user.id, assignment_id=queue.day.assignment_id
    )
    lines = [
        f"Задание: {assignment.title}",
        f"Дата: {queue.day.date:%d.%m.%Y} (Москва)",
        f"Записано: {queue.occupied} из {queue.day.capacity}.",
    ]
    lines.extend(f"{member.position}. {member.student_name}" for member in queue.members.items)
    if not queue.members.items:
        lines.append("На этой странице записей нет.")
    await send_text(
        message,
        "\n".join(lines),
        reply_markup=selection_keyboard(
            [
                (
                    "Обновить",
                    TeacherQueueCallback(
                        action="open", object_id=defense_day_id, offset=offset
                    ).pack(),
                ),
                ("Дни защиты", TeacherQueueCallback(action="days", object_id=assignment.id).pack()),
            ],
            previous=TeacherQueueCallback(
                action="open", object_id=defense_day_id, offset=max(0, offset - PAGE_SIZE)
            ).pack()
            if offset
            else None,
            following=TeacherQueueCallback(
                action="open", object_id=defense_day_id, offset=offset + PAGE_SIZE
            ).pack()
            if queue.members.has_next
            else None,
        ),
    )
