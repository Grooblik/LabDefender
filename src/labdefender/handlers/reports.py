"""Reports as CSV files sent to the chat, for the bot and the Mini App alike.

CSV with `;` and a UTF-8 BOM opens in Excel with the Russian locale as is. Every value that
users typed (names, titles) is guarded against spreadsheet formulas.
"""

import csv
import io
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role, SubmissionStatus
from labdefender.handlers.coursework import answer_no_groups
from labdefender.keyboards.coursework import selection_keyboard
from labdefender.keyboards.reports import ReportCallback
from labdefender.services.errors import StaleCourseAction
from labdefender.services.groups import list_groups
from labdefender.services.pagination import PAGE_SIZE
from labdefender.services.queue import today_moscow
from labdefender.services.reports import (
    GroupReport,
    StudentReport,
    group_report,
    moscow,
    student_report,
)

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe(value: object) -> str:
    text = "" if value is None else str(value)
    # A leading =, +, -, @ would make a spreadsheet run the cell as a formula.
    return "'" + text if text.startswith(FORMULA_START) else text


def _csv(rows: list[list[object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerows([[safe(value) for value in row] for row in rows])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def group_report_csv(report: GroupReport) -> bytes:
    rows: list[list[object]] = [
        [
            "Студент",
            "Username",
            *(assignment.title for assignment in report.assignments),
            "Допущено",
        ]
    ]
    for student in report.students:
        rows.append(
            [
                student.full_name,
                student.username or "",
                *(report.cell(student.id, assignment.id).full for assignment in report.assignments),
                f"{report.approved_count(student.id)} из {len(report.assignments)}",
            ]
        )
    return _csv(rows)


def student_report_csv(report: StudentReport) -> bytes:
    rows: list[list[object]] = [
        ["Дисциплина", "Задание", "Статус", "Попыток", "Последняя попытка", "Допущено", "Защита"]
    ]
    for discipline in report.disciplines:
        for assignment, cell in discipline.rows:
            rows.append(
                [
                    discipline.group.name,
                    assignment.title,
                    cell.status_label,
                    cell.attempts or "",
                    moscow(cell.submitted_at),
                    moscow(cell.approved_at),
                    cell.defense_label,
                ]
            )
    return _csv(rows)


def report_filename(kind: str, name: str) -> str:
    slug = re.sub(r"[^\w-]+", "_", name, flags=re.UNICODE).strip("_")[:40] or kind
    return f"{kind}-{slug}-{today_moscow():%Y-%m-%d}.csv"


def group_caption(report: GroupReport) -> str:
    approved = sum(report.approved_count(student.id) for student in report.students)
    total = len(report.students) * len(report.assignments)
    return (
        f"Отчёт по группе «{report.group.name}»: студентов {len(report.students)}, "
        f"заданий {len(report.assignments)}, допущено работ {approved} из {total}."
    )


def student_caption(report: StudentReport) -> str:
    lines = [f"Отчёт: {report.student.full_name}"]
    for discipline in report.disciplines:
        approved = sum(cell.status == SubmissionStatus.APPROVED for _, cell in discipline.rows)
        lines.append(f"{discipline.group.name}: допущено {approved} из {len(discipline.rows)}")
    if not report.disciplines:
        lines.append("Вы пока не состоите в группах.")
    return "\n".join(lines)[:1024]


async def send_report(bot: Bot, chat_id: int, data: bytes, filename: str, caption: str) -> None:
    await bot.send_document(
        chat_id, BufferedInputFile(data, filename=filename), caption=caption[:1024]
    )


async def send_group_report(bot: Bot, session: AsyncSession, user: User, *, group_id: int) -> None:
    report = await group_report(session, actor_id=user.id, group_id=group_id)
    await send_report(
        bot,
        user.telegram_id,
        group_report_csv(report),
        report_filename("group", report.group.name),
        group_caption(report),
    )


async def send_student_report(
    bot: Bot, session: AsyncSession, user: User, *, student_id: int | None = None
) -> None:
    report = await student_report(session, actor_id=user.id, student_id=student_id)
    await send_report(
        bot,
        user.telegram_id,
        student_report_csv(report),
        report_filename("student", report.student.full_name),
        student_caption(report),
    )


async def show_report_groups(
    message: Message, session: AsyncSession, user: User, *, offset: int = 0
) -> None:
    page = await list_groups(session, actor_id=user.id, offset=offset)
    if not page.groups and not offset:
        await answer_no_groups(message, user)
        return
    buttons = [
        (f"📊 {group.name}", ReportCallback(kind="group", object_id=group.id).pack())
        for group in page.groups
    ]
    await message.answer(
        "Отчёт по какой группе прислать? Таблица придёт файлом CSV (открывается в Excel).",
        reply_markup=selection_keyboard(
            buttons,
            previous=ReportCallback(kind="groups", object_id=max(0, offset - PAGE_SIZE)).pack()
            if offset
            else None,
            following=ReportCallback(kind="groups", object_id=offset + PAGE_SIZE).pack()
            if page.has_next
            else None,
        ),
    )


def create_router() -> Router:
    router = Router(name="reports")

    @router.message(Command("report"))
    async def report_command(
        message: Message, session: AsyncSession, current_user: User, bot: Bot
    ) -> None:
        if current_user.role == Role.TEACHER:
            await show_report_groups(message, session, current_user)
        else:
            await send_student_report(bot, session, current_user)

    @router.callback_query(ReportCallback.filter(F.kind == "groups"))
    async def report_groups(
        callback: CallbackQuery,
        callback_data: ReportCallback,
        session: AsyncSession,
        current_user: User,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        await show_report_groups(
            callback.message, session, current_user, offset=callback_data.object_id
        )
        await callback.answer()

    @router.callback_query(ReportCallback.filter(F.kind == "group"))
    async def report_group(
        callback: CallbackQuery,
        callback_data: ReportCallback,
        session: AsyncSession,
        current_user: User,
        bot: Bot,
    ) -> None:
        await send_group_report(bot, session, current_user, group_id=callback_data.object_id)
        await callback.answer("Отчёт отправлен файлом.")

    @router.callback_query(ReportCallback.filter(F.kind == "me"))
    async def report_me(
        callback: CallbackQuery, session: AsyncSession, current_user: User, bot: Bot
    ) -> None:
        await send_student_report(bot, session, current_user)
        await callback.answer("Отчёт отправлен файлом.")

    return router
