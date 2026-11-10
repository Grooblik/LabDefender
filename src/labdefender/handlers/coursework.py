from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import AssignmentFormat, Role, SubmissionStatus
from labdefender.keyboards.coursework import (
    StudentCourseCallback,
    TeacherCourseCallback,
    selection_keyboard,
)
from labdefender.keyboards.defense import StudentQueueCallback, TeacherQueueCallback
from labdefender.services.assignments import get_assignment, list_assignments
from labdefender.services.errors import SolutionDeliveryUnavailable, StaleCourseAction
from labdefender.services.groups import get_group, list_groups
from labdefender.services.pagination import PAGE_SIZE
from labdefender.services.submissions import (
    STATUS_LABELS,
    get_submission,
    latest_submission,
    list_submissions,
)

FORMAT_LABELS = {
    AssignmentFormat.ONLINE: "онлайн",
    AssignmentFormat.IN_PERSON: "очная",
}


def format_label(value: AssignmentFormat) -> str:
    return FORMAT_LABELS.get(value, value.value)


async def send_text(
    message: Message, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    # Telegram limits text in UTF-16 units; never split an astral character in half.
    chunks, buffer, units = [], [], 0
    for character in text:
        weight = 2 if ord(character) > 0xFFFF else 1
        if units + weight > 4000:
            chunks.append("".join(buffer))
            buffer, units = [], 0
        buffer.append(character)
        units += weight
    chunks.append("".join(buffer))
    for index, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            parse_mode=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


async def require_draft(state: FSMContext, *, expected_state: str, nonce: str) -> dict:
    data = await state.get_data()
    if await state.get_state() != expected_state or data.get("nonce") != nonce:
        raise StaleCourseAction()
    return data


async def show_course_groups(
    message: Message, session: AsyncSession, user: User, *, mode: str, offset: int = 0
) -> None:
    page = await list_groups(session, actor_id=user.id, offset=offset)
    teacher = user.role == Role.TEACHER
    factory = TeacherCourseCallback if teacher else StudentCourseCallback
    action = ("publish" if mode == "publish" else "review_list") if teacher else "list"
    page_action = (
        ("groups_publish" if mode == "publish" else "groups_review") if teacher else "groups"
    )
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


async def show_assignments(
    message: Message, session: AsyncSession, user: User, *, group_id: int, offset: int = 0
) -> None:
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    page = await list_assignments(session, actor_id=user.id, group_id=group_id, offset=offset)
    teacher = user.role == Role.TEACHER
    factory = TeacherCourseCallback if teacher else StudentCourseCallback
    action, page_action = ("attempts", "review_list") if teacher else ("open", "list")
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


async def show_student_assignment(
    message: Message, session: AsyncSession, user: User, *, assignment_id: int
) -> None:
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    group = await get_group(session, actor_id=user.id, group_id=assignment.group_id)
    latest = await latest_submission(session, actor_id=user.id, assignment_id=assignment_id)
    status = STATUS_LABELS[latest.status] if latest else "Не отправлено"
    buttons = []
    if latest is None or latest.status == SubmissionStatus.RETURNED:
        buttons.append(
            (
                "Отправить решение" if latest is None else "Пересдать",
                StudentCourseCallback(action="submit", object_id=assignment_id).pack(),
            )
        )
    if latest:
        buttons.append(
            ("Мои попытки", StudentCourseCallback(action="history", object_id=assignment_id).pack())
        )
        if latest.status == SubmissionStatus.APPROVED:
            buttons.append(
                (
                    "Защита и очередь",
                    StudentQueueCallback(action="status", object_id=assignment_id).pack(),
                )
            )
    buttons.append(
        (
            "Задания группы",
            StudentCourseCallback(action="list", object_id=assignment.group_id).pack(),
        )
    )
    await send_text(
        message,
        f"Группа: {group.name}\nЗадание: {assignment.title}\n"
        f"Формат сдачи: {format_label(assignment.format)}\n\n{assignment.description}",
    )
    comment = (
        f"\nКомментарий преподавателя: {latest.review_comment}"
        if latest and latest.review_comment
        else ""
    )
    await send_text(message, f"Статус: {status}{comment}", reply_markup=selection_keyboard(buttons))


async def show_submissions(
    message: Message, session: AsyncSession, user: User, *, assignment_id: int, offset: int = 0
) -> None:
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    page = await list_submissions(
        session, actor_id=user.id, assignment_id=assignment_id, offset=offset
    )
    teacher = user.role == Role.TEACHER
    factory = TeacherCourseCallback if teacher else StudentCourseCallback
    action, page_action = ("open", "attempts") if teacher else ("attempt", "history")
    extra_buttons = (
        [
            (
                "Слоты защиты и очередь",
                TeacherQueueCallback(action="days", object_id=assignment_id).pack(),
            )
        ]
        if teacher
        else []
    )
    await send_text(
        message,
        f"Задание: {assignment.title}\nФормат сдачи: {format_label(assignment.format)}\n"
        + ("Выберите попытку:" if page.items else "Попыток на этой странице нет."),
        reply_markup=selection_keyboard(
            [
                (
                    f"#{item.submission.id} · {item.student_name[:18]} · "
                    f"№{item.submission.attempt_number} · {STATUS_LABELS[item.submission.status]}",
                    factory(action=action, object_id=item.submission.id).pack(),
                )
                for item in page.items
            ]
            + extra_buttons,
            previous=factory(
                action=page_action, object_id=assignment_id, offset=max(0, offset - PAGE_SIZE)
            ).pack()
            if offset
            else None,
            following=factory(
                action=page_action, object_id=assignment_id, offset=offset + PAGE_SIZE
            ).pack()
            if page.has_next
            else None,
        ),
    )


async def show_submission(
    message: Message, session: AsyncSession, user: User, *, submission_id: int
) -> None:
    detail = await get_submission(session, actor_id=user.id, submission_id=submission_id)
    submission = detail.submission
    buttons = []
    if user.role == Role.TEACHER:
        if submission.status == SubmissionStatus.PENDING:
            buttons.extend(
                [
                    (
                        "Допустить",
                        TeacherCourseCallback(action="approve", object_id=submission_id).pack(),
                    ),
                    (
                        "На доработку",
                        TeacherCourseCallback(action="return", object_id=submission_id).pack(),
                    ),
                ]
            )
        buttons.append(
            (
                "Попытки задания",
                TeacherCourseCallback(action="attempts", object_id=submission.assignment_id).pack(),
            )
        )
    else:
        buttons.append(
            (
                "К заданию",
                StudentCourseCallback(action="open", object_id=submission.assignment_id).pack(),
            )
        )
    comment = f"\nКомментарий: {submission.review_comment}" if submission.review_comment else ""
    await send_text(
        message,
        f"Группа: {detail.group.name}\nЗадание: {detail.assignment.title}\n"
        f"Формат сдачи: {format_label(detail.assignment.format)}\n"
        f"Студент: {detail.student.full_name}\nПопытка №{submission.attempt_number}\n"
        f"Статус: {STATUS_LABELS[submission.status]}{comment}",
    )
    keyboard = selection_keyboard(buttons)
    if submission.file_id:
        try:
            await message.answer_document(document=submission.file_id, reply_markup=keyboard)
        except TelegramBadRequest:
            raise SolutionDeliveryUnavailable() from None
    else:
        await send_text(message, submission.content, reply_markup=keyboard)
