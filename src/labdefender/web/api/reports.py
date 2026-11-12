"""Reports in the Mini App: a discipline table, a student's report, the same as a CSV file.

The file goes to the requester's chat with the bot, as other files do: WebView downloads are
unreliable on phones.
"""

from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from labdefender.enums import SubmissionStatus
from labdefender.handlers.reports import send_group_report, send_student_report
from labdefender.services.reports import WorkCell, group_report, moscow, student_report
from labdefender.web.context import BOT, ChatUnavailable, current_user, db, path_id

routes = web.RouteTableDef()


def cell_json(cell: WorkCell) -> dict:
    return {
        "status": cell.status.value if cell.status else None,
        "status_label": cell.status_label,
        "attempts": cell.attempts,
        "submitted_label": moscow(cell.submitted_at) or None,
        "approved_label": moscow(cell.approved_at) or None,
        "defense_label": cell.defense_label or None,
        "short": cell.short,
        "full": cell.full,
    }


def person_json(user) -> dict:
    return {"id": user.id, "name": user.full_name, "username": user.username}


@routes.get(r"/api/groups/{id:\d+}/report")
async def group_table(request: web.Request) -> web.Response:
    """Teacher of the group: students × assignments."""
    report = await group_report(
        db(request), actor_id=current_user(request).id, group_id=path_id(request)
    )
    assignments = report.assignments
    return web.json_response(
        {
            "group": {"id": report.group.id, "name": report.group.name},
            "assignments": [{"id": item.id, "title": item.title} for item in assignments],
            "rows": [
                {
                    "student": person_json(student),
                    "approved": report.approved_count(student.id),
                    "cells": [cell_json(report.cell(student.id, item.id)) for item in assignments],
                }
                for student in report.students
            ],
            "totals": [
                {
                    "approved": sum(
                        report.cell(student.id, item.id).status == SubmissionStatus.APPROVED
                        for student in report.students
                    ),
                    "submitted": sum(
                        report.cell(student.id, item.id).status is not None
                        for student in report.students
                    ),
                }
                for item in assignments
            ],
        }
    )


async def _student_json(request: web.Request, student_id: int | None) -> web.Response:
    report = await student_report(
        db(request), actor_id=current_user(request).id, student_id=student_id
    )
    return web.json_response(
        {
            "student": person_json(report.student),
            "disciplines": [
                {
                    "group": {"id": item.group.id, "name": item.group.name},
                    "approved": sum(
                        cell.status == SubmissionStatus.APPROVED for _, cell in item.rows
                    ),
                    "total": len(item.rows),
                    "rows": [
                        {
                            "assignment": {"id": assignment.id, "title": assignment.title},
                            **cell_json(cell),
                        }
                        for assignment, cell in item.rows
                    ],
                }
                for item in report.disciplines
            ],
        }
    )


@routes.get(r"/api/students/{id:\d+}/report")
async def student_for_teacher(request: web.Request) -> web.Response:
    """Teacher: a student of own groups, only in those groups."""
    return await _student_json(request, path_id(request))


@routes.get("/api/me/report")
async def own_report(request: web.Request) -> web.Response:
    return await _student_json(request, None)


async def _deliver(send) -> web.Response:
    try:
        await send()
    except TelegramAPIError:
        raise ChatUnavailable() from None
    return web.json_response({"sent": True})


@routes.post(r"/api/groups/{id:\d+}/report/file")
async def group_file(request: web.Request) -> web.Response:
    session, user, group_id = db(request), current_user(request), path_id(request)
    return await _deliver(
        lambda: send_group_report(request.app[BOT], session, user, group_id=group_id)
    )


@routes.post(r"/api/students/{id:\d+}/report/file")
async def student_file(request: web.Request) -> web.Response:
    session, user, student_id = db(request), current_user(request), path_id(request)
    return await _deliver(
        lambda: send_student_report(request.app[BOT], session, user, student_id=student_id)
    )


@routes.post("/api/me/report/file")
async def own_file(request: web.Request) -> web.Response:
    session, user = db(request), current_user(request)
    return await _deliver(lambda: send_student_report(request.app[BOT], session, user))
