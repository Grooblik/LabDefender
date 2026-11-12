"""Groups and assignments: lists, cards, creating a group and publishing an assignment."""

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from labdefender.db.models import Assignment, Submission, User
from labdefender.enums import Role, SubmissionStatus
from labdefender.handlers.coursework import format_label
from labdefender.handlers.notifications import notify_assignment_published
from labdefender.services.assignments import create_assignment, get_assignment, list_assignments
from labdefender.services.groups import (
    count_members,
    create_group,
    get_group,
    get_invite_link,
    list_groups,
    list_members,
)
from labdefender.services.submissions import latest_submission
from labdefender.web.api.records import submission_json
from labdefender.web.context import (
    BOT,
    IDEMPOTENCY,
    NOTIFIER,
    current_user,
    db,
    page_json,
    path_id,
    query_offset,
    read_body,
)

routes = web.RouteTableDef()


class GroupDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(strict=True)


class AssignmentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(strict=True)
    description: str = Field(strict=True)
    format: str = Field(strict=True)


def assignment_json(assignment: Assignment) -> dict:
    return {
        "id": assignment.id,
        "group_id": assignment.group_id,
        "title": assignment.title,
        "format": assignment.format.value,
        "format_label": format_label(assignment.format),
    }


def student_state(latest: Submission | None) -> dict:
    """What a student sees about an assignment: the latest attempt and what can be done."""
    return {
        "latest": submission_json(latest) if latest else None,
        "status_label": submission_json(latest)["status_label"] if latest else "Не отправлено",
        "can_submit": latest is None or latest.status == SubmissionStatus.RETURNED,
        "approved": latest is not None and latest.status == SubmissionStatus.APPROVED,
    }


async def _student_items(request: web.Request, user: User, items) -> list[dict]:
    result = []
    for assignment in items:
        item = assignment_json(assignment)
        if user.role == Role.STUDENT:
            latest = await latest_submission(
                db(request), actor_id=user.id, assignment_id=assignment.id
            )
            item["status_label"] = student_state(latest)["status_label"]
        result.append(item)
    return result


@routes.get("/api/groups")
async def groups(request: web.Request) -> web.Response:
    page = await list_groups(
        db(request), actor_id=current_user(request).id, offset=query_offset(request)
    )
    return web.json_response(
        {
            "items": [{"id": group.id, "name": group.name} for group in page.groups],
            "offset": page.offset,
            "has_next": page.has_next,
        }
    )


@routes.post("/api/groups")
async def new_group(request: web.Request) -> web.Response:
    session, user = db(request), current_user(request)
    draft = await read_body(request, GroupDraft)

    async def create() -> dict:
        group = await create_group(session, actor_id=user.id, name=draft.name)
        return {"group": {"id": group.id, "name": group.name}}

    body = await request.app[IDEMPOTENCY].run(
        user.id, request.headers.get("Idempotency-Key"), create
    )
    return web.json_response(body, status=201)


@routes.get(r"/api/groups/{id:\d+}")
async def group_card(request: web.Request) -> web.Response:
    session, user, group_id = db(request), current_user(request), path_id(request)
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    body = {
        "group": {"id": group.id, "name": group.name},
        "members": await count_members(session, actor_id=user.id, group_id=group_id),
    }
    if user.role == Role.TEACHER:
        username = (await request.app[BOT].me()).username or ""
        body["invite_link"] = await get_invite_link(
            session, actor_id=user.id, group_id=group_id, bot_username=username
        )
    return web.json_response(body)


@routes.get(r"/api/groups/{id:\d+}/members")
async def group_members(request: web.Request) -> web.Response:
    """Teacher only: each student's chosen or Telegram name and Telegram username."""
    session, user, group_id = db(request), current_user(request), path_id(request)
    page = await list_members(
        session, actor_id=user.id, group_id=group_id, offset=query_offset(request)
    )
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    body = page_json(
        page,
        [
            {"id": member.id, "name": member.full_name, "username": member.username}
            for member in page.items
        ],
    )
    body["group"] = {"id": group.id, "name": group.name}
    return web.json_response(body)


@routes.get(r"/api/groups/{id:\d+}/assignments")
async def group_assignments(request: web.Request) -> web.Response:
    session, user, group_id = db(request), current_user(request), path_id(request)
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    page = await list_assignments(
        session, actor_id=user.id, group_id=group_id, offset=query_offset(request)
    )
    return web.json_response(
        {
            "group": {"id": group.id, "name": group.name},
            **page_json(page, await _student_items(request, user, page.items)),
        }
    )


@routes.post(r"/api/groups/{id:\d+}/assignments")
async def publish(request: web.Request) -> web.Response:
    session, user, group_id = db(request), current_user(request), path_id(request)
    draft = await read_body(request, AssignmentDraft)

    async def create() -> dict:
        assignment = await create_assignment(
            session,
            actor_id=user.id,
            group_id=group_id,
            title=draft.title,
            description=draft.description,
            format=draft.format,
        )
        await notify_assignment_published(
            request.app[NOTIFIER], request.app[BOT], session, assignment_id=assignment.id
        )
        return {"assignment": assignment_json(assignment)}

    body = await request.app[IDEMPOTENCY].run(
        user.id, request.headers.get("Idempotency-Key"), create
    )
    return web.json_response(body, status=201)


@routes.get(r"/api/assignments/{id:\d+}")
async def assignment_card(request: web.Request) -> web.Response:
    session, user, assignment_id = db(request), current_user(request), path_id(request)
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    group = await get_group(session, actor_id=user.id, group_id=assignment.group_id)
    body = {
        "assignment": {**assignment_json(assignment), "description": assignment.description},
        "group": {"id": group.id, "name": group.name},
    }
    if user.role == Role.STUDENT:
        latest = await latest_submission(session, actor_id=user.id, assignment_id=assignment_id)
        body.update(student_state(latest))
    return web.json_response(body)
