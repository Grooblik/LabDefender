import asyncio
import re
from datetime import UTC, datetime

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from aiogram.types import Update
from sqlalchemy import func, select

from labdefender.app import create_dispatcher
from labdefender.db.models import Assignment, Membership, Submission
from labdefender.db.session import create_database, initialize_database
from labdefender.enums import AssignmentFormat, Role, SubmissionStatus
from labdefender.keyboards.coursework import (
    StudentCourseCallback,
    StudentDraftCallback,
    TeacherCourseCallback,
)
from labdefender.services.assignments import create_assignment, text_units
from labdefender.services.errors import (
    DomainError,
    InvalidAssignment,
    InvalidReviewComment,
    InvalidSolution,
    SubmissionConflict,
)
from labdefender.services.groups import create_group
from labdefender.services.submissions import (
    MAX_FILE_SIZE,
    document_solution,
    get_submission,
    latest_submission,
    list_submissions,
    review_submission,
    submit_solution,
    text_solution,
)
from labdefender.services.users import ensure_user


def message_update(text=None, *, user_id=101, document=None, photo=False):
    message = {
        "message_id": 10,
        "date": datetime.now(UTC),
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}"},
    }
    if text is not None:
        message["text"] = text
        if re.match(r"^/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?(?:\s|$)", text):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
    if document:
        message["document"] = document
    if photo:
        message["photo"] = [
            {"file_id": "photo", "file_unique_id": "photo-unique", "width": 10, "height": 10}
        ]
    return Update.model_validate({"update_id": 1, "message": message})


def callback_update(data, *, user_id=101):
    message = message_update("Menu", user_id=user_id).message.model_dump(by_alias=True)
    message["from"] = {"id": 123456, "is_bot": True, "first_name": "Bot"}
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "course-callback",
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


def doc(*, extension="pdf", size=10, mime="application/octet-stream"):
    return {
        "file_id": f"telegram-{extension}",
        "file_unique_id": "unique",
        "file_name": f"solution.{extension}",
        "file_size": size,
        "mime_type": mime,
    }


@pytest.fixture
async def course(database):
    _, sessions = database
    ids = {}
    async with sessions() as session:
        for telegram_id, role in [
            (101, Role.TEACHER),
            (202, Role.STUDENT),
            (203, Role.STUDENT),
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
        other_group = await create_group(session, actor_id=ids[303], name="Private group")
    async with sessions() as session:
        session.add_all(
            [
                Membership(group_id=group.id, student_id=ids[202]),
                Membership(group_id=group.id, student_id=ids[203]),
            ]
        )
        await session.commit()
    async with sessions() as session:
        assignment = await create_assignment(
            session,
            actor_id=ids[101],
            group_id=group.id,
            title="ЛР 1",
            description="Условие",
            format=AssignmentFormat.ONLINE,
        )
    async with sessions() as session:
        other_assignment = await create_assignment(
            session,
            actor_id=ids[303],
            group_id=other_group.id,
            title="Private lab",
            description="Private task",
            format=AssignmentFormat.ONLINE,
        )
    return sessions, ids, group, assignment, other_assignment


async def submit(course, *, solution=None, previous=None, student=202):
    sessions, ids, _, assignment, _ = course
    async with sessions() as session:
        return await submit_solution(
            session,
            actor_id=ids[student],
            assignment_id=assignment.id,
            expected_previous_id=previous,
            solution=solution or text_solution("Решение"),
        )


async def review(course, submission_id, decision, *, comment=None, teacher=101):
    sessions, ids, *_ = course
    async with sessions() as session:
        return await review_submission(
            session,
            actor_id=ids[teacher],
            submission_id=submission_id,
            decision=decision,
            comment=comment,
        )


async def test_return_resubmit_approve_preserves_history_and_approval_time(course):
    sessions, ids, _, assignment, _ = course
    first = await submit(course)
    assert first.attempt_number == 1
    await review(course, first.id, SubmissionStatus.RETURNED, comment="Исправьте вывод")
    second = await submit(course, previous=first.id)
    assert second.attempt_number == 2
    approved = await review(course, second.id, SubmissionStatus.APPROVED)
    approved_at = approved.approved_at
    with pytest.raises(SubmissionConflict):
        await review(course, second.id, SubmissionStatus.APPROVED)
    with pytest.raises(SubmissionConflict):
        await review(course, first.id, SubmissionStatus.RETURNED, comment="Old button")
    with pytest.raises(SubmissionConflict):
        await submit(course, previous=second.id)
    async with sessions() as session:
        latest = await latest_submission(session, actor_id=ids[202], assignment_id=assignment.id)
        assert latest.approved_at == approved_at
        history = await list_submissions(session, actor_id=ids[202], assignment_id=assignment.id)
        assert [item.submission.id for item in history.items] == [second.id, first.id]
        assert history.items[1].submission.review_comment == "Исправьте вывод"


async def test_pending_attempt_and_stale_draft_cannot_be_submitted_again(course):
    first = await submit(course)
    with pytest.raises(SubmissionConflict):
        await submit(course)
    await review(course, first.id, SubmissionStatus.RETURNED, comment="Fix")
    second = await submit(course, previous=first.id)
    await review(course, second.id, SubmissionStatus.RETURNED, comment="Fix again")
    with pytest.raises(SubmissionConflict):
        await submit(course, previous=first.id)


@pytest.mark.parametrize("comment", [None, " \n ", "a" * 4001])
async def test_return_requires_valid_comment_without_changing_pending_attempt(course, comment):
    submission = await submit(course)
    with pytest.raises(InvalidReviewComment):
        await review(course, submission.id, SubmissionStatus.RETURNED, comment=comment)
    sessions, ids, *_ = course
    async with sessions() as session:
        assert (
            await get_submission(session, actor_id=ids[101], submission_id=submission.id)
        ).submission.status == SubmissionStatus.PENDING


async def test_permissions_protect_assignments_and_solution_contents(course):
    sessions, ids, group, assignment, private_assignment = course
    submission = await submit(course)
    for actor in (202, 303, 404):
        async with sessions() as session:
            with pytest.raises(DomainError):
                await create_assignment(
                    session,
                    actor_id=ids[actor],
                    group_id=group.id,
                    title="Forged",
                    description="Forged",
                    format=AssignmentFormat.ONLINE,
                )
    for actor in (203, 303, 404):
        async with sessions() as session:
            with pytest.raises(DomainError):
                await get_submission(session, actor_id=ids[actor], submission_id=submission.id)
    async with sessions() as session:
        assert not (
            await list_submissions(session, actor_id=ids[203], assignment_id=assignment.id)
        ).items
    async with sessions() as session:
        with pytest.raises(DomainError):
            await submit_solution(
                session,
                actor_id=ids[202],
                assignment_id=private_assignment.id,
                expected_previous_id=None,
                solution=text_solution("Forged"),
            )
    with pytest.raises(DomainError):
        await review(course, submission.id, SubmissionStatus.APPROVED, teacher=303)


async def test_parallel_submissions_allow_only_one_pending_attempt(course):
    results = await asyncio.gather(submit(course), submit(course), return_exceptions=True)
    assert sum(isinstance(result, Submission) for result in results) == 1
    assert sum(isinstance(result, SubmissionConflict) for result in results) == 1


async def test_parallel_review_cannot_overwrite_winning_decision(course):
    submission = await submit(course)
    results = await asyncio.gather(
        review(course, submission.id, SubmissionStatus.APPROVED),
        review(course, submission.id, SubmissionStatus.RETURNED, comment="Fix"),
        return_exceptions=True,
    )
    winners = [result for result in results if isinstance(result, Submission)]
    assert len(winners) == 1
    assert sum(isinstance(result, SubmissionConflict) for result in results) == 1
    sessions, ids, *_ = course
    async with sessions() as session:
        stored = (
            await get_submission(session, actor_id=ids[202], submission_id=submission.id)
        ).submission
        assert stored.status == winners[0].status


@pytest.mark.parametrize(
    "solution",
    [
        text_solution("Text"),
        text_solution("https://example.com/solution"),
        document_solution(
            file_id="pdf-id",
            file_name="report.PDF",
            file_size=MAX_FILE_SIZE,
            mime_type="application/pdf",
        ),
        document_solution(
            file_id="zip-id", file_name="source.zip", file_size=10, mime_type="application/zip"
        ),
    ],
)
async def test_supported_solution_types_round_trip_in_database(course, solution):
    submission = await submit(course, solution=solution)
    sessions, ids, *_ = course
    async with sessions() as session:
        stored = (
            await get_submission(session, actor_id=ids[101], submission_id=submission.id)
        ).submission
        assert stored.kind == solution.kind
        assert stored.content == solution.content
        assert stored.file_id == solution.file_id


@pytest.mark.parametrize(
    "changes",
    [
        {"file_size": None},
        {"file_size": 0},
        {"file_size": MAX_FILE_SIZE + 1},
        {"file_name": "report.exe"},
        {"mime_type": "application/zip"},
        {"file_name": None},
    ],
)
def test_invalid_documents_are_rejected(changes):
    values = {
        "file_id": "file-id",
        "file_name": "report.pdf",
        "file_size": 10,
        "mime_type": "application/pdf",
    }
    values.update(changes)
    with pytest.raises(InvalidSolution):
        document_solution(**values)


@pytest.mark.parametrize(
    "url", ["https://", "https://example.com/a b", "https://example.com:bad", "https://[broken"]
)
def test_invalid_http_links_are_rejected(url):
    with pytest.raises(InvalidSolution):
        text_solution(url)


def test_text_limit_counts_utf16_units():
    assert text_solution("😀" * 2048).content == "😀" * 2048
    with pytest.raises(InvalidSolution):
        text_solution("😀" * 2049)


async def test_submitted_text_preserves_indentation_and_newlines(course):
    source = "    return result\n\n"
    submission = await submit(course, solution=text_solution(source))
    sessions, ids, *_ = course
    async with sessions() as session:
        assert (
            await get_submission(session, actor_id=ids[101], submission_id=submission.id)
        ).submission.content == source


async def test_cpp_comment_is_plain_text_in_submission_dialogue(course, bot):
    sessions, ids, _, assignment, _ = course
    dispatcher = create_dispatcher(sessions)
    source = "// Solution\n    return result;\n"
    confirmation = await prepare_solution(dispatcher, bot, assignment.id, text=source)
    await press(dispatcher, bot, confirmation, user=202)
    async with sessions() as session:
        stored = await latest_submission(session, actor_id=ids[202], assignment_id=assignment.id)
        assert stored.content == source


@pytest.mark.parametrize(
    "title,description", [("", "Task"), ("a" * 257, "Task"), ("Lab", " "), ("Lab", "😀" * 2001)]
)
async def test_invalid_assignment_is_not_published(course, title, description):
    sessions, ids, group, *_ = course
    async with sessions() as session:
        with pytest.raises(InvalidAssignment):
            await create_assignment(
                session,
                actor_id=ids[101],
                group_id=group.id,
                title=title,
                description=description,
                format=AssignmentFormat.ONLINE,
            )


async def test_solutions_and_review_survive_reopen(course, database):
    sessions, ids, _, assignment, _ = course
    submission = await submit(course)
    await review(course, submission.id, SubmissionStatus.RETURNED, comment="Persisted comment")
    old_engine, _ = database
    engine, reopened = create_database(str(old_engine.url))
    try:
        await initialize_database(engine)
        async with reopened() as session:
            stored = await latest_submission(
                session, actor_id=ids[202], assignment_id=assignment.id
            )
            assert stored.review_comment == "Persisted comment"
            assert stored.status == SubmissionStatus.RETURNED
    finally:
        await engine.dispose()


async def press(dispatcher, bot, callback, *, user=101):
    await dispatcher.feed_update(
        bot,
        callback_update(callback.pack() if hasattr(callback, "pack") else callback, user_id=user),
    )


async def prepare_solution(dispatcher, bot, assignment_id, *, text="Решение", document=None):
    await press(
        dispatcher, bot, StudentCourseCallback(action="submit", object_id=assignment_id), user=202
    )
    await dispatcher.feed_update(
        bot, message_update(text if document is None else None, user_id=202, document=document)
    )
    return first_button(bot)


async def test_full_bot_flow_publish_return_resubmit_file_approve(course, bot):
    sessions, ids, group, *_ = course
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/new_assignment"))
    await press(dispatcher, bot, first_button(bot))
    await press(dispatcher, bot, first_button(bot))
    await dispatcher.feed_update(bot, message_update("ЛР из диалога"))
    await dispatcher.feed_update(bot, message_update("Условие из диалога"))
    confirmation = first_button(bot)
    await press(dispatcher, bot, confirmation)
    await press(dispatcher, bot, confirmation)
    assert "устарела" in bot.session.calls[-1].text
    async with sessions() as session:
        assignment = await session.scalar(
            select(Assignment).where(Assignment.title == "ЛР из диалога")
        )
        assert assignment.group_id == group.id
        assert assignment.format == AssignmentFormat.IN_PERSON
        assignment_id = assignment.id
    await dispatcher.feed_update(bot, message_update("/assignments", user_id=202))
    await press(dispatcher, bot, first_button(bot), user=202)
    await press(
        dispatcher, bot, StudentCourseCallback(action="open", object_id=assignment_id), user=202
    )
    assert "Формат сдачи: очная" in messages(bot)[-2].text + messages(bot)[-1].text
    assert "Не отправлено" in messages(bot)[-1].text
    confirmation = await prepare_solution(dispatcher, bot, assignment_id)
    await press(dispatcher, bot, confirmation, user=202)
    await press(dispatcher, bot, confirmation, user=202)
    assert "устарела" in bot.session.calls[-1].text
    async with sessions() as session:
        first = await latest_submission(session, actor_id=ids[202], assignment_id=assignment_id)
        first_id = first.id
    await dispatcher.feed_update(bot, message_update("/submissions"))
    await press(dispatcher, bot, first_button(bot))
    await press(dispatcher, bot, TeacherCourseCallback(action="attempts", object_id=assignment_id))
    await press(dispatcher, bot, first_button(bot))
    assert messages(bot)[-1].text == "Решение"
    await press(dispatcher, bot, TeacherCourseCallback(action="return", object_id=first_id))
    await dispatcher.feed_update(bot, message_update(" "))
    assert "Для возврата нужен комментарий" in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("Исправьте расчёт"))
    await press(
        dispatcher, bot, StudentCourseCallback(action="open", object_id=assignment_id), user=202
    )
    assert "Исправьте расчёт" in messages(bot)[-1].text
    confirmation = await prepare_solution(
        dispatcher, bot, assignment_id, document=doc(extension="zip")
    )
    await press(dispatcher, bot, confirmation, user=202)
    async with sessions() as session:
        second = await latest_submission(session, actor_id=ids[202], assignment_id=assignment_id)
        second_id = second.id
    await press(dispatcher, bot, TeacherCourseCallback(action="open", object_id=second_id))
    assert any(
        isinstance(call, SendDocument) and call.document == "telegram-zip"
        for call in bot.session.calls
    )
    await press(dispatcher, bot, TeacherCourseCallback(action="approve", object_id=second_id))
    await press(dispatcher, bot, TeacherCourseCallback(action="approve", object_id=first_id))
    assert "устарела" in bot.session.calls[-1].text
    await press(
        dispatcher, bot, StudentCourseCallback(action="open", object_id=assignment_id), user=202
    )
    assert "Допущено" in messages(bot)[-1].text
    await press(
        dispatcher, bot, StudentCourseCallback(action="submit", object_id=assignment_id), user=202
    )
    assert "уже отправлена" in bot.session.calls[-1].text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Submission)) == 2


@pytest.mark.parametrize("solution_type", ["pdf", "zip", "text", "link"])
async def test_each_solution_type_can_be_sent_and_shown_without_downloading(
    course, bot, solution_type
):
    sessions, ids, _, assignment, _ = course
    dispatcher = create_dispatcher(sessions)
    document = doc(extension=solution_type) if solution_type in {"pdf", "zip"} else None
    text = "https://example.com/work" if solution_type == "link" else "Text work"
    confirmation = await prepare_solution(
        dispatcher, bot, assignment.id, text=text, document=document
    )
    await press(dispatcher, bot, confirmation, user=202)
    async with sessions() as session:
        latest = await latest_submission(session, actor_id=ids[202], assignment_id=assignment.id)
        assert latest.kind.value == solution_type
        submission_id = latest.id
    await press(dispatcher, bot, TeacherCourseCallback(action="open", object_id=submission_id))
    if document:
        assert bot.session.calls[-2].document == document["file_id"]
    else:
        assert messages(bot)[-1].text == text


async def test_invalid_upload_can_be_corrected_and_other_commands_are_not_solutions(course, bot):
    sessions, _, _, assignment, _ = course
    dispatcher = create_dispatcher(sessions)
    await press(
        dispatcher, bot, StudentCourseCallback(action="submit", object_id=assignment.id), user=202
    )
    await dispatcher.feed_update(bot, message_update(user_id=202, photo=True))
    assert "Отправьте один PDF" in messages(bot)[-1].text
    await dispatcher.feed_update(
        bot, message_update(user_id=202, document=doc(size=MAX_FILE_SIZE + 1))
    )
    assert "Отправьте один PDF" in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("/help", user_id=202))
    assert "LabDefender" in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("Correct text", user_id=202))
    await press(dispatcher, bot, first_button(bot), user=202)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Submission)) == 1


@pytest.mark.parametrize("cancel", ["/cancel", "restart", "button"])
async def test_cancelled_submission_draft_cannot_create_attempt(course, bot, cancel):
    sessions, _, _, assignment, _ = course
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_solution(dispatcher, bot, assignment.id)
    if cancel == "restart":
        dispatcher = create_dispatcher(sessions)
    elif cancel == "button":
        data = StudentDraftCallback.unpack(confirmation)
        data.action = "cancel"
        await press(dispatcher, bot, data, user=202)
    else:
        await dispatcher.feed_update(bot, message_update(cancel, user_id=202))
    await press(dispatcher, bot, confirmation, user=202)
    assert "устарела" in bot.session.calls[-1].text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Submission)) == 0


async def test_forged_callbacks_never_send_private_solution(course, bot):
    sessions, *_ = course
    submission = await submit(
        course,
        solution=document_solution(
            file_id="private-file", file_name="private.pdf", file_size=10, mime_type=None
        ),
    )
    dispatcher = create_dispatcher(sessions)
    for user, callback in [
        (303, TeacherCourseCallback(action="open", object_id=submission.id)),
        (203, StudentCourseCallback(action="attempt", object_id=submission.id)),
        (202, TeacherCourseCallback(action="approve", object_id=submission.id)),
    ]:
        await press(dispatcher, bot, callback, user=user)
        assert isinstance(bot.session.calls[-1], AnswerCallbackQuery)
    assert not any(isinstance(call, SendDocument) for call in bot.session.calls)
    assert not any("private-file" in call.text for call in messages(bot))


async def test_long_unicode_solution_and_description_are_not_truncated(course, bot):
    sessions, ids, group, *_ = course
    description = "😀" * 2000
    async with sessions() as session:
        assignment = await create_assignment(
            session,
            actor_id=ids[101],
            group_id=group.id,
            title="a" * 256,
            description=description,
            format=AssignmentFormat.ONLINE,
        )
    dispatcher = create_dispatcher(sessions)
    await press(
        dispatcher, bot, StudentCourseCallback(action="open", object_id=assignment.id), user=202
    )
    assert description in "".join(call.text for call in messages(bot))
    solution = "😀" * 2048
    confirmation = await prepare_solution(dispatcher, bot, assignment.id, text=solution)
    await press(dispatcher, bot, confirmation, user=202)
    async with sessions() as session:
        submission = await latest_submission(
            session, actor_id=ids[202], assignment_id=assignment.id
        )
        submission_id = submission.id
    start = len(bot.session.calls)
    await press(dispatcher, bot, TeacherCourseCallback(action="open", object_id=submission_id))
    rendered = "".join(
        call.text for call in bot.session.calls[start:] if isinstance(call, SendMessage)
    )
    assert solution in rendered
    assert all(text_units(call.text) <= 4096 for call in messages(bot))


async def test_delivery_failure_after_submission_commit_does_not_duplicate(
    course, bot, monkeypatch
):
    sessions, _, _, assignment, _ = course
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_solution(dispatcher, bot, assignment.id)
    original = bot.session.make_request

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, SendMessage) and "отправлена на проверку" in method.text:
            raise TelegramNetworkError(method=method, message="Example delivery failure")
        return await original(bot, method, timeout)

    with monkeypatch.context() as patch:
        patch.setattr(bot.session, "make_request", request)
        with pytest.raises(TelegramNetworkError):
            await press(dispatcher, bot, confirmation, user=202)
    await press(dispatcher, bot, confirmation, user=202)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Submission)) == 1
