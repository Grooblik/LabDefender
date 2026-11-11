"""Attempts: student submission (text or link here, a file through the chat) and review.

Files are never uploaded through the page: the solution stays a Telegram `file_id`. The file
button starts the same chat step as the chat's own button, and a stored file is sent to the
user's chat on request instead of being downloaded by the server.
"""

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from labdefender.enums import Role, SubmissionStatus
from labdefender.handlers.notifications import (
    notify_submission_received,
    notify_submission_reviewed,
)
from labdefender.handlers.student.coursework import begin_solution, solution_prompt
from labdefender.keyboards.menu import cancel_keyboard
from labdefender.services.errors import SolutionDeliveryUnavailable
from labdefender.services.submissions import (
    get_submission,
    list_pending_reviews,
    list_submissions,
    review_submission,
    submit_solution,
    text_solution,
)
from labdefender.web.api.courses import assignment_json
from labdefender.web.api.records import submission_json
from labdefender.web.context import (
    BOT,
    IDEMPOTENCY,
    NOTIFIER,
    ChatUnavailable,
    InvalidRequest,
    chat_state,
    current_user,
    db,
    page_json,
    path_id,
    query_offset,
    read_body,
)

routes = web.RouteTableDef()


class TextAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(strict=True)
    expected_previous_id: int | None = Field(strict=True)


class ReturnComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str = Field(strict=True)


@routes.get(r"/api/assignments/{id:\d+}/submissions")
async def attempts(request: web.Request) -> web.Response:
    page = await list_submissions(
        db(request),
        actor_id=current_user(request).id,
        assignment_id=path_id(request),
        offset=query_offset(request),
    )
    items = [
        submission_json(item.submission, student_name=item.student_name) for item in page.items
    ]
    return web.json_response(page_json(page, items))


@routes.post(r"/api/assignments/{id:\d+}/submissions")
async def submit_text(request: web.Request) -> web.Response:
    session, user, assignment_id = db(request), current_user(request), path_id(request)
    answer = await read_body(request, TextAnswer)
    solution = text_solution(answer.text)

    async def submit() -> dict:
        submission = await submit_solution(
            session,
            actor_id=user.id,
            assignment_id=assignment_id,
            expected_previous_id=answer.expected_previous_id,
            solution=solution,
        )
        await notify_submission_received(
            request.app[NOTIFIER], request.app[BOT], session, submission_id=submission.id
        )
        await session.refresh(submission)  # Load the server-side submission time.
        return {"submission": submission_json(submission)}

    body = await request.app[IDEMPOTENCY].run(
        user.id, request.headers.get("Idempotency-Key"), submit
    )
    return web.json_response(body, status=201)


@routes.post(r"/api/assignments/{id:\d+}/file-request")
async def request_file(request: web.Request) -> web.Response:
    session, user, assignment_id = db(request), current_user(request), path_id(request)

    async def ask() -> dict:
        state = chat_state(request, user)
        assignment = await begin_solution(session, user, state, assignment_id=assignment_id)
        try:
            await request.app[BOT].send_message(
                user.telegram_id, solution_prompt(assignment), reply_markup=cancel_keyboard()
            )
        except TelegramAPIError:
            await state.clear()
            raise ChatUnavailable() from None
        return {"requested": True}

    body = await request.app[IDEMPOTENCY].run(user.id, request.headers.get("Idempotency-Key"), ask)
    return web.json_response(body)


@routes.get(r"/api/submissions/{id:\d+}")
async def attempt(request: web.Request) -> web.Response:
    user = current_user(request)
    detail = await get_submission(db(request), actor_id=user.id, submission_id=path_id(request))
    submission = detail.submission
    teacher = user.role == Role.TEACHER
    return web.json_response(
        {
            "submission": {
                **submission_json(submission, student_name=detail.student.full_name),
                "content": submission.content,
                "student_username": detail.student.username,
            },
            "assignment": assignment_json(detail.assignment),
            "group": {"id": detail.group.id, "name": detail.group.name},
            # Only a pending attempt (always the latest one) can be reviewed.
            "can_review": teacher and submission.status == SubmissionStatus.PENDING,
        }
    )


@routes.post(r"/api/submissions/{id:\d+}/send-file")
async def send_file(request: web.Request) -> web.Response:
    user = current_user(request)
    detail = await get_submission(db(request), actor_id=user.id, submission_id=path_id(request))
    submission = detail.submission
    if submission.file_id is None:
        raise InvalidRequest("У этой попытки нет файла.")
    caption = (
        f"{detail.assignment.title} · {detail.student.full_name} · "
        f"попытка №{submission.attempt_number}"
    )
    try:
        await request.app[BOT].send_document(
            user.telegram_id, document=submission.file_id, caption=caption[:1024]
        )
    except TelegramBadRequest:
        raise SolutionDeliveryUnavailable() from None
    except TelegramAPIError:
        raise ChatUnavailable() from None
    return web.json_response({"sent": True})


@routes.get("/api/review")
async def review_inbox(request: web.Request) -> web.Response:
    page = await list_pending_reviews(
        db(request), actor_id=current_user(request).id, offset=query_offset(request)
    )
    return web.json_response(
        page_json(
            page,
            [
                {
                    **submission_json(item.submission, student_name=item.student_name),
                    "assignment": assignment_json(item.assignment),
                    "group_name": item.group_name,
                }
                for item in page.items
            ],
        )
    )


async def _review(request: web.Request, decision: SubmissionStatus, comment: str | None = None):
    session, user, submission_id = db(request), current_user(request), path_id(request)
    submission = await review_submission(
        session, actor_id=user.id, submission_id=submission_id, decision=decision, comment=comment
    )
    # A repeated tap fails in the service above, so the student is notified once.
    await notify_submission_reviewed(
        request.app[NOTIFIER], request.app[BOT], session, submission_id=submission_id
    )
    return web.json_response({"submission": submission_json(submission)})


@routes.post(r"/api/submissions/{id:\d+}/approve")
async def approve(request: web.Request) -> web.Response:
    return await _review(request, SubmissionStatus.APPROVED)


@routes.post(r"/api/submissions/{id:\d+}/return")
async def send_back(request: web.Request) -> web.Response:
    body = await read_body(request, ReturnComment)
    return await _review(request, SubmissionStatus.RETURNED, body.comment)
