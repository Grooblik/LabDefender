import pytest
import test_queue
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import SendDocument, SendMessage
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr
from sqlalchemy import func, select
from test_coursework import doc, message_update
from test_queue import book, day
from test_web import TOKEN, init_data

from labdefender.app import create_dispatcher
from labdefender.db.models import Assignment, StudyGroup, Submission
from labdefender.enums import AssignmentFormat, SubmissionKind, SubmissionStatus
from labdefender.handlers.notifications import Notifier
from labdefender.services.assignments import create_assignment
from labdefender.services.submissions import submit_solution, text_solution
from labdefender.states.coursework import SolutionSubmission
from labdefender.web.app import create_web_app

# Teacher 101 owns «ИВТ-101» with «ЛР 1», where students 202-204 are approved.
# Teacher 303 and student 404 have no groups. Moscow time is fixed at 2026-11-10 10:00.
context = test_queue.context
TEACHER, OTHER_TEACHER, STUDENT, OUTSIDER = 101, 303, 202, 404


@pytest.fixture
async def setup(context, bot):
    sessions, *_ = context
    # The dispatcher and the web app share the FSM storage, as in run().
    dispatcher = create_dispatcher(sessions)
    app = create_web_app(
        sessions,
        bot_token=SecretStr(TOKEN),
        auth_max_age=3600,
        bot=bot,
        notifier=Notifier(),
        storage=dispatcher.storage,
    )
    async with TestClient(TestServer(app)) as client:
        yield client, dispatcher


async def call(client, method, path, *, user=TEACHER, status=200, **kwargs):
    # Signed with the fixture's names: every API call refreshes the user's name, as /start does.
    signed = init_data(user_id=user, name=f"User{user}")
    headers = {"Authorization": f"tma {signed}", **kwargs.pop("headers", {})}
    response = await client.request(method, f"/api{path}", headers=headers, **kwargs)
    body = await response.json()
    assert response.status == status, body
    return body


def sent(bot, chat_id=None):
    return [
        call
        for call in bot.session.calls
        if isinstance(call, SendMessage) and (chat_id is None or int(call.chat_id) == chat_id)
    ]


async def fresh_assignment(context, title="ЛР 2"):
    sessions, ids, group, *_ = context
    async with sessions() as session:
        return await create_assignment(
            session,
            actor_id=ids[TEACHER],
            group_id=group.id,
            title=title,
            description="Условие\nв две строки",
            format=AssignmentFormat.IN_PERSON,
        )


async def pending(context, assignment, *, student=STUDENT, text="Работа"):
    sessions, ids, *_ = context
    async with sessions() as session:
        return await submit_solution(
            session,
            actor_id=ids[student],
            assignment_id=assignment.id,
            expected_previous_id=None,
            solution=text_solution(text),
        )


async def test_student_sees_assignments_with_status_and_card(context, setup):
    client, _ = setup
    _, _, group, approved, _ = context
    fresh = await fresh_assignment(context)
    listing = await call(client, "GET", f"/groups/{group.id}/assignments", user=STUDENT)
    assert [(item["title"], item["status_label"]) for item in listing["items"]] == [
        ("ЛР 1", "Допущено"),
        ("ЛР 2", "Не отправлено"),
    ]
    card = await call(client, "GET", f"/assignments/{fresh.id}", user=STUDENT)
    assert card["assignment"]["description"] == "Условие\nв две строки"
    assert card["group"] == {"id": group.id, "name": "ИВТ-101"}
    assert (card["latest"], card["can_submit"], card["approved"]) == (None, True, False)
    done = await call(client, "GET", f"/assignments/{approved.id}", user=STUDENT)
    assert (done["can_submit"], done["approved"], done["latest"]["status"]) == (
        False,
        True,
        "approved",
    )
    teacher_card = await call(client, "GET", f"/assignments/{fresh.id}")
    assert "latest" not in teacher_card
    assert (
        "status_label"
        not in (await call(client, "GET", f"/groups/{group.id}/assignments"))["items"][0]
    )
    await call(client, "GET", f"/assignments/{fresh.id}", user=OUTSIDER, status=404)


async def test_text_answer_notifies_teacher_once_and_checks_version(context, setup, bot):
    client, _ = setup
    assignment = await fresh_assignment(context)
    path = f"/assignments/{assignment.id}/submissions"
    body = {"text": "  Решение\n  с отступом", "expected_previous_id": None}
    created = await call(
        client,
        "POST",
        path,
        user=STUDENT,
        json=body,
        headers={"Idempotency-Key": "answer-01"},
        status=201,
    )
    submission = created["submission"]
    assert (submission["attempt_number"], submission["status"], submission["kind"]) == (
        1,
        "pending",
        "text",
    )
    assert submission["submitted_label"]
    [notice] = sent(bot, TEACHER)
    assert notice.text.startswith("Новое решение на проверку.")
    repeated = await call(
        client,
        "POST",
        path,
        user=STUDENT,
        json=body,
        headers={"Idempotency-Key": "answer-01"},
        status=201,
    )
    assert repeated == created and len(sent(bot, TEACHER)) == 1
    stale = await call(client, "POST", path, user=STUDENT, json=body, status=409)
    assert "Попытка уже отправлена" in stale["error"]
    await call(client, "POST", path, user=STUDENT, json={**body, "text": "   "}, status=422)
    await call(client, "POST", path, user=TEACHER, json=body, status=403)
    sessions, *_ = context
    async with sessions() as session:
        stored = await session.scalar(
            select(Submission).where(Submission.assignment_id == assignment.id)
        )
        assert stored.content == "  Решение\n  с отступом"


async def test_link_answer_is_stored_as_link(context, setup):
    client, _ = setup
    assignment = await fresh_assignment(context)
    created = await call(
        client,
        "POST",
        f"/assignments/{assignment.id}/submissions",
        user=STUDENT,
        json={"text": " https://example.com/repo ", "expected_previous_id": None},
        status=201,
    )
    assert created["submission"]["kind"] == "link"


async def test_file_button_continues_in_chat(context, setup, bot):
    client, dispatcher = setup
    sessions, *_ = context
    assignment = await fresh_assignment(context)
    await call(client, "POST", f"/assignments/{assignment.id}/file-request", user=STUDENT)
    key = StorageKey(bot_id=bot.id, chat_id=STUDENT, user_id=STUDENT)
    assert await dispatcher.storage.get_state(key) == SolutionSubmission.payload.state
    [prompt] = sent(bot, STUDENT)
    assert (
        prompt.text == "Задание: ЛР 2\nОтправьте один PDF/ZIP до 20 МиБ, текст или ссылку HTTP(S)."
    )
    assert prompt.reply_markup.inline_keyboard[0][0].text == "Отмена"
    # The student sends the file in the chat and confirms there, as after the chat button.
    await dispatcher.feed_update(bot, message_update(user_id=STUDENT, document=doc()))
    confirm = sent(bot, STUDENT)[-1].reply_markup.inline_keyboard[0][0].callback_data
    await dispatcher.feed_update(bot, test_queue.callback_update(confirm, user_id=STUDENT))
    async with sessions() as session:
        stored = await session.scalar(
            select(Submission).where(Submission.assignment_id == assignment.id)
        )
        assert (stored.kind, stored.file_id) == (SubmissionKind.PDF, "telegram-pdf")
    # Pending now: the file button is refused like in the chat.
    await call(
        client, "POST", f"/assignments/{assignment.id}/file-request", user=STUDENT, status=409
    )


async def test_file_button_reports_closed_chat_and_drops_the_step(context, setup, bot, monkeypatch):
    client, dispatcher = setup
    assignment = await fresh_assignment(context)
    original = bot.session.make_request

    async def blocked(bot_, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, SendMessage):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked")
        return await original(bot_, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", blocked)
    body = await call(
        client, "POST", f"/assignments/{assignment.id}/file-request", user=STUDENT, status=409
    )
    assert "Не удалось написать вам в чат" in body["error"]
    key = StorageKey(bot_id=bot.id, chat_id=STUDENT, user_id=STUDENT)
    assert await dispatcher.storage.get_state(key) is None


async def test_attempts_detail_and_file_to_chat(context, setup, bot):
    client, _ = setup
    sessions, ids, _, approved, submissions = context
    own = await call(client, "GET", f"/assignments/{approved.id}/submissions", user=STUDENT)
    assert [item["student_name"] for item in own["items"]] == ["User202"]
    everyone = await call(client, "GET", f"/assignments/{approved.id}/submissions")
    assert len(everyone["items"]) == 3
    detail = await call(client, "GET", f"/submissions/{submissions[STUDENT]}")
    assert detail["submission"]["content"] == "Work"
    assert (detail["can_review"], detail["group"]["name"]) == (False, "ИВТ-101")
    await call(client, "GET", f"/submissions/{submissions[203]}", user=STUDENT, status=404)
    no_file = await call(
        client, "POST", f"/submissions/{submissions[STUDENT]}/send-file", status=422
    )
    assert no_file["error"] == "У этой попытки нет файла."

    assignment = await fresh_assignment(context)
    async with sessions() as session:
        from labdefender.services.submissions import document_solution

        file_attempt = await submit_solution(
            session,
            actor_id=ids[STUDENT],
            assignment_id=assignment.id,
            expected_previous_id=None,
            solution=document_solution(
                file_id="private-file", file_name="work.zip", file_size=10, mime_type=None
            ),
        )
    detail = await call(client, "GET", f"/submissions/{file_attempt.id}")
    assert (detail["submission"]["has_file"], detail["submission"]["content"]) == (True, None)
    await call(client, "POST", f"/submissions/{file_attempt.id}/send-file")
    await call(client, "POST", f"/submissions/{file_attempt.id}/send-file", user=STUDENT)
    documents = [c for c in bot.session.calls if isinstance(c, SendDocument)]
    assert [(int(d.chat_id), d.document) for d in documents] == [
        (TEACHER, "private-file"),
        (STUDENT, "private-file"),
    ]
    await call(client, "POST", f"/submissions/{file_attempt.id}/send-file", user=203, status=404)


async def test_review_inbox_approve_and_return_notify_once(context, setup, bot):
    client, _ = setup
    first = await fresh_assignment(context, "ЛР 2")
    second = await fresh_assignment(context, "ЛР 3")
    older = await pending(context, first, student=203)
    newer = await pending(context, second, student=STUDENT)
    inbox = await call(client, "GET", "/review")
    assert [
        (item["id"], item["assignment"]["title"], item["group_name"]) for item in inbox["items"]
    ] == [
        (older.id, "ЛР 2", "ИВТ-101"),
        (newer.id, "ЛР 3", "ИВТ-101"),
    ]
    assert (await call(client, "GET", "/review", user=OTHER_TEACHER))["items"] == []
    await call(client, "GET", "/review", user=STUDENT, status=403)
    assert (await call(client, "GET", f"/submissions/{older.id}"))["can_review"] is True

    approved = await call(client, "POST", f"/submissions/{older.id}/approve")
    assert approved["submission"]["status"] == "approved"
    assert sent(bot, 203)[-1].text.startswith("Работа допущена.")
    again = await call(client, "POST", f"/submissions/{older.id}/approve", status=409)
    assert "проверена" in again["error"]
    assert len(sent(bot, 203)) == 1

    await call(
        client, "POST", f"/submissions/{newer.id}/return", json={"comment": "  "}, status=422
    )
    returned = await call(
        client, "POST", f"/submissions/{newer.id}/return", json={"comment": "Добавьте тесты"}
    )
    assert (returned["submission"]["status"], returned["submission"]["comment"]) == (
        "returned",
        "Добавьте тесты",
    )
    assert "Добавьте тесты" in sent(bot, STUDENT)[-1].text
    await call(client, "POST", f"/submissions/{newer.id}/approve", user=OTHER_TEACHER, status=404)
    assert (await call(client, "GET", "/review"))["items"] == []


async def test_student_books_switches_and_moves_with_versions(context, setup, bot):
    client, _ = setup
    _, _, _, assignment, _ = context
    first = await day(context, capacity=3)
    second = await day(context, start_time=test_queue.time(17), end_time=test_queue.time(18))
    await book(context, first, student=203)
    path = f"/assignments/{assignment.id}/booking"
    assert (await call(client, "GET", path, user=STUDENT))["booking"] is None
    slots = await call(client, "GET", f"/groups/{context[2].id}/slots", user=STUDENT)
    assert [item["id"] for item in slots["items"]] == [first.id, second.id]
    works = await call(client, "GET", f"/groups/{context[2].id}/defense", user=STUDENT)
    assert [
        (work["assignment"]["id"], work["attempt_number"], work["booking"])
        for work in works["works"]
    ] == [(assignment.id, 1, None)]

    nothing = {"expected_entry_id": None, "expected_order_at": None}
    enroll = f"/assignments/{assignment.id}/enroll"
    booked = (
        await call(client, "POST", enroll, user=STUDENT, json={**nothing, "slot_id": first.id})
    )["booking"]
    # Approved earlier than 203, so first in the queue.
    assert (booked["slot"]["id"], booked["position"], booked["occupied"]) == (first.id, 1, 2)
    assert sent(bot, TEACHER)[-1].text.startswith("Запись на защиту")
    version = {"expected_entry_id": booked["entry_id"], "expected_order_at": booked["order_at"]}
    same = await call(client, "POST", enroll, user=STUDENT, json={**version, "slot_id": first.id})
    assert same["booking"]["position"] == 1 and len(sent(bot, TEACHER)) == 1
    stale = await call(
        client, "POST", enroll, user=STUDENT, json={**nothing, "slot_id": second.id}, status=409
    )
    assert "запись изменилась" in stale["error"]

    moved = await call(
        client, "POST", f"/assignments/{assignment.id}/move-to-end", user=STUDENT, json=version
    )
    assert moved["booking"]["position"] == 2
    version = {
        "expected_entry_id": moved["booking"]["entry_id"],
        "expected_order_at": moved["booking"]["order_at"],
    }
    switched = await call(
        client, "POST", enroll, user=STUDENT, json={**version, "slot_id": second.id}
    )
    assert switched["booking"]["slot"]["id"] == second.id
    assert sent(bot, TEACHER)[-1].text.startswith("Смена слота защиты")
    await call(
        client,
        "POST",
        f"/assignments/{assignment.id}/move-to-end",
        user=STUDENT,
        json=nothing,
        status=422,
    )

    fresh = await fresh_assignment(context)
    refused = await call(
        client, "GET", f"/assignments/{fresh.id}/booking", user=STUDENT, status=409
    )
    assert "нужен допуск" in refused["error"]


async def test_teacher_creates_group_and_sees_invite(context, setup):
    client, _ = setup
    sessions, *_ = context
    created = await call(
        client,
        "POST",
        "/groups",
        json={"name": "  ПМИ-201  "},
        headers={"Idempotency-Key": "group-key-1"},
        status=201,
    )
    again = await call(
        client,
        "POST",
        "/groups",
        json={"name": "  ПМИ-201  "},
        headers={"Idempotency-Key": "group-key-1"},
        status=201,
    )
    assert created == again and created["group"]["name"] == "ПМИ-201"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 2
    await call(client, "POST", "/groups", json={"name": ""}, status=422)
    await call(client, "POST", "/groups", user=STUDENT, json={"name": "Своя"}, status=403)

    _, _, group, *_ = context
    card = await call(client, "GET", f"/groups/{group.id}")
    assert card["members"] == 3
    assert card["invite_link"].startswith("https://t.me/LabDefenderTestBot?start=join_")
    student_card = await call(client, "GET", f"/groups/{group.id}", user=STUDENT)
    assert student_card["members"] == 3 and "invite_link" not in student_card
    await call(client, "GET", f"/groups/{group.id}", user=OTHER_TEACHER, status=404)


async def test_publishing_notifies_group_once_and_validates(context, setup, bot):
    client, _ = setup
    sessions, _, group, *_ = context
    path = f"/groups/{group.id}/assignments"
    body = {"title": "ЛР 5", "description": "Сделать отчёт", "format": "in_person"}
    created = await call(
        client, "POST", path, json=body, headers={"Idempotency-Key": "publish-1"}, status=201
    )
    assert created["assignment"]["format_label"] == "очная"
    assert sorted(int(c.chat_id) for c in sent(bot)) == [202, 203, 204]
    await call(
        client, "POST", path, json=body, headers={"Idempotency-Key": "publish-1"}, status=201
    )
    assert len(sent(bot)) == 3
    await call(client, "POST", path, json={**body, "format": "remote"}, status=422)
    await call(client, "POST", path, json={**body, "title": " "}, status=422)
    await call(client, "POST", path, json=body, user=OTHER_TEACHER, status=404)
    async with sessions() as session:
        titles = (await session.scalars(select(Assignment.title).order_by(Assignment.id))).all()
        assert titles == ["ЛР 1", "ЛР 5"]
        statuses = set((await session.scalars(select(Submission.status))).all())
        assert statuses == {SubmissionStatus.APPROVED}
