"""Reports: a discipline table and a student's report — rights, cells, CSV, chat and API."""

import csv
import io
from datetime import timedelta

import pytest
import test_queue
from aiogram.methods import SendDocument
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr
from test_queue import NOW, book, day, message_update, press
from test_web import TOKEN
from test_web_coursework import call

from labdefender.app import create_dispatcher
from labdefender.db.models import Membership, Submission
from labdefender.enums import AssignmentFormat, SubmissionStatus
from labdefender.handlers.notifications import Notifier
from labdefender.handlers.reports import group_report_csv, student_report_csv
from labdefender.keyboards.reports import ReportCallback
from labdefender.services.assignments import create_assignment
from labdefender.services.errors import AccessDenied, ObjectUnavailable
from labdefender.services.groups import create_group
from labdefender.services.reports import group_report, moscow, student_report
from labdefender.services.submissions import review_submission, submit_solution, text_solution
from labdefender.services.users import set_display_name
from labdefender.web.app import create_web_app

# Teacher 101: «ИВТ-101» with ЛР 1 approved for 202-204. Teacher 303 and student 404: no groups.
context = test_queue.context
TEACHER, OTHER_TEACHER, STUDENT, OUTSIDER = 101, 303, 202, 404


async def service(context, function, **kwargs):
    sessions, *_ = context
    async with sessions() as session:
        return await function(session, **kwargs)


@pytest.fixture
async def progress(context):
    """ЛР 2: 202 on review, 203 returned twice then on review; 204 nothing. 202 booked ЛР 1."""
    sessions, ids, group, *_ = context
    second = await service(
        context,
        create_assignment,
        actor_id=ids[TEACHER],
        group_id=group.id,
        title="ЛР 2",
        description="Task",
        format=AssignmentFormat.ONLINE,
    )
    await service(
        context,
        submit_solution,
        actor_id=ids[202],
        assignment_id=second.id,
        expected_previous_id=None,
        solution=text_solution("Work"),
    )
    previous = None
    for _ in range(2):
        attempt = await service(
            context,
            submit_solution,
            actor_id=ids[203],
            assignment_id=second.id,
            expected_previous_id=previous,
            solution=text_solution("Work"),
        )
        await service(
            context,
            review_submission,
            actor_id=ids[TEACHER],
            submission_id=attempt.id,
            decision=SubmissionStatus.RETURNED,
            comment="Ещё раз",
        )
        previous = attempt.id
    slot = await day(context)
    await book(context, slot, student=202)
    return second, slot


async def test_group_report_shows_status_attempts_dates_and_defense(context, progress):
    sessions, ids, group, first, _ = context
    second, slot = progress
    report = await service(context, group_report, actor_id=ids[TEACHER], group_id=group.id)
    assert [student.full_name for student in report.students] == ["User202", "User203", "User204"]
    assert [assignment.title for assignment in report.assignments] == ["ЛР 1", "ЛР 2"]
    booked = report.cell(ids[202], first.id)
    assert (booked.status, booked.attempts, booked.slot.id) == (
        SubmissionStatus.APPROVED,
        1,
        slot.id,
    )
    approved_day = moscow(NOW - timedelta(hours=3), "%d.%m")  # 202 was approved first.
    assert booked.short == f"✅ {approved_day} · 🗓 {slot.date:%d.%m}"
    assert "защита" in booked.full and "попыток: 1" in booked.full
    returned = report.cell(ids[203], second.id)
    assert (returned.status, returned.attempts, returned.short) == (
        SubmissionStatus.RETURNED,
        2,
        "🔄 ×2",
    )
    assert report.cell(ids[202], second.id).short.startswith("⏳ ")
    nothing = report.cell(ids[204], second.id)
    assert (nothing.status, nothing.short, nothing.full) == (None, "—", "Не сдано")
    assert [report.approved_count(ids[number]) for number in (202, 203, 204)] == [1, 1, 1]

    with pytest.raises(AccessDenied):
        await service(context, group_report, actor_id=ids[STUDENT], group_id=group.id)
    with pytest.raises(ObjectUnavailable):
        await service(context, group_report, actor_id=ids[OTHER_TEACHER], group_id=group.id)


async def test_student_report_spans_own_groups_and_teachers_see_only_theirs(context, progress):
    sessions, ids, group, *_ = context
    other = await service(context, create_group, actor_id=ids[OTHER_TEACHER], name="Базы данных")
    await service(
        context,
        create_assignment,
        actor_id=ids[OTHER_TEACHER],
        group_id=other.id,
        title="БД 1",
        description="Task",
        format=AssignmentFormat.ONLINE,
    )
    async with sessions() as session:
        session.add(Membership(group_id=other.id, student_id=ids[202]))
        await session.commit()

    own = await service(context, student_report, actor_id=ids[202])
    assert [item.group.name for item in own.disciplines] == ["Базы данных", "ИВТ-101"]
    assert [assignment.title for assignment, _ in own.disciplines[1].rows] == ["ЛР 1", "ЛР 2"]
    # Each teacher sees the student only in the teacher's own disciplines.
    for teacher, names in ((TEACHER, ["ИВТ-101"]), (OTHER_TEACHER, ["Базы данных"])):
        report = await service(context, student_report, actor_id=ids[teacher], student_id=ids[202])
        assert [item.group.name for item in report.disciplines] == names
    with pytest.raises(ObjectUnavailable):
        await service(context, student_report, actor_id=ids[TEACHER], student_id=ids[OUTSIDER])
    with pytest.raises(AccessDenied):
        await service(context, student_report, actor_id=ids[202], student_id=ids[203])


def read_csv(data: bytes) -> list[list[str]]:
    assert data.startswith("﻿".encode())
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))


async def test_csv_opens_in_excel_and_never_runs_formulas(context, progress):
    sessions, ids, group, *_ = context
    await service(context, set_display_name, actor_id=ids[204], name='=HYPERLINK("x")')
    report = await service(context, group_report, actor_id=ids[TEACHER], group_id=group.id)
    rows = read_csv(group_report_csv(report))
    assert rows[0] == ["Студент", "Username", "ЛР 1", "ЛР 2", "Допущено"]
    # Sorted by name: the renamed student comes first; the formula is shown as text.
    assert [row[0] for row in rows[1:]] == ['\'=HYPERLINK("x")', "User202", "User203"]
    assert rows[3][3].startswith("На доработке, попыток: 2")
    assert rows[1][3] == "Не сдано" and rows[1][4] == "1 из 2"

    student = await service(context, student_report, actor_id=ids[202])
    rows = read_csv(student_report_csv(student))
    assert rows[0][:3] == ["Дисциплина", "Задание", "Статус"]
    assert [row[1:3] for row in rows[1:]] == [["ЛР 1", "Допущено"], ["ЛР 2", "На проверке"]]
    assert rows[1][6]  # The defense slot of ЛР 1.


def documents(bot, chat_id):
    return [
        call
        for call in bot.session.calls
        if isinstance(call, SendDocument) and int(call.chat_id) == chat_id
    ]


async def test_reports_arrive_in_the_chat_as_files(context, progress, bot):
    sessions, _, group, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/report"))
    choice = bot.session.calls[-1]
    labels = [button.text for row in choice.reply_markup.inline_keyboard for button in row]
    assert labels == ["📊 ИВТ-101"]
    await press(dispatcher, bot, ReportCallback(kind="group", object_id=group.id))
    [sent] = documents(bot, TEACHER)
    assert sent.document.filename.startswith("group-ИВТ-101-") and sent.document.filename.endswith(
        ".csv"
    )
    assert "студентов 3, заданий 2, допущено работ 3 из 6" in sent.caption

    await dispatcher.feed_update(bot, message_update("/report", user_id=STUDENT))
    [own] = documents(bot, STUDENT)
    assert own.caption == "Отчёт: User202\nИВТ-101: допущено 1 из 2"
    # Another teacher's group is not available, even by a crafted button.
    await press(dispatcher, bot, ReportCallback(kind="group", object_id=group.id), user=303)
    assert documents(bot, 303) == []


@pytest.fixture
async def client(context, bot):
    sessions, *_ = context
    app = create_web_app(
        sessions,
        bot_token=SecretStr(TOKEN),
        auth_max_age=3600,
        bot=bot,
        notifier=Notifier(),
        storage=create_dispatcher(sessions).storage,
    )
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def test_report_api_matches_the_service_and_sends_files(context, progress, client, bot):
    _, ids, group, first, _ = context
    table = await call(client, "GET", f"/groups/{group.id}/report")
    assert [item["title"] for item in table["assignments"]] == ["ЛР 1", "ЛР 2"]
    assert [row["student"]["name"] for row in table["rows"]] == ["User202", "User203", "User204"]
    assert [cell["status"] for cell in table["rows"][1]["cells"]] == ["approved", "returned"]
    assert table["totals"] == [{"approved": 3, "submitted": 3}, {"approved": 0, "submitted": 2}]
    assert table["rows"][0]["cells"][0]["defense_label"]

    student_id = table["rows"][0]["student"]["id"]
    seen = await call(client, "GET", f"/students/{student_id}/report")
    assert [item["group"]["name"] for item in seen["disciplines"]] == ["ИВТ-101"]
    own = await call(client, "GET", "/me/report", user=STUDENT)
    assert (own["disciplines"][0]["approved"], own["disciplines"][0]["total"]) == (1, 2)

    await call(client, "GET", f"/groups/{group.id}/report", user=STUDENT, status=403)
    await call(client, "GET", f"/groups/{group.id}/report", user=OTHER_TEACHER, status=404)
    await call(client, "GET", f"/students/{student_id}/report", user=OTHER_TEACHER, status=404)
    await call(client, "GET", f"/students/{student_id}/report", user=203, status=403)

    for path, user in (
        (f"/groups/{group.id}/report/file", TEACHER),
        (f"/students/{student_id}/report/file", TEACHER),
        ("/me/report/file", STUDENT),
    ):
        assert await call(client, "POST", path, user=user) == {"sent": True}
    assert len(documents(bot, TEACHER)) == 2 and len(documents(bot, STUDENT)) == 1
    async with context[0]() as session:
        assert await session.get(Submission, 1) is not None
