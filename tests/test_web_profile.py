"""Stage 5 of the Mini App: profile, teacher code, role grant, notification settings, app links."""

import pytest
import test_queue
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import ReplyKeyboardMarkup
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr
from sqlalchemy import select
from test_queue import message_update
from test_web import TOKEN, TUNNEL, settings
from test_web_coursework import call, fresh_assignment, pending, sent

from labdefender.app import create_dispatcher
from labdefender.db.models import StudyGroup, User
from labdefender.enums import Role
from labdefender.handlers.notifications import Notifier
from labdefender.keyboards.menu import MENU_LAYOUT
from labdefender.services.groups import create_group
from labdefender.services.notifications import teacher_preferences
from labdefender.states.registration import TeacherRegistration
from labdefender.web import mini_app_url
from labdefender.web.app import create_web_app

# Teacher 101 owns «ИВТ-101» with approved students 202-204; teacher 303 and student 404 have
# no groups. Teacher 303 is the administrator here.
context = test_queue.context
TEACHER, ADMIN, STUDENT, OUTSIDER = 101, 303, 202, 404
CODE = "secret-teacher-code"


@pytest.fixture
async def setup(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    app = create_web_app(
        sessions,
        bot_token=SecretStr(TOKEN),
        auth_max_age=3600,
        bot=bot,
        notifier=Notifier(webapp_url=TUNNEL),
        storage=dispatcher.storage,
        admin_ids=frozenset({ADMIN}),
        registration_code=SecretStr(CODE),
    )
    async with TestClient(TestServer(app)) as client:
        yield client, dispatcher


def app_buttons(message):
    return [
        button.web_app.url
        for row in message.reply_markup.inline_keyboard
        for button in row
        if button.web_app
    ]


def labels(message):
    return [button.text for row in message.reply_markup.inline_keyboard for button in row]


async def test_profile_tells_admins_and_students_who_may_enter_the_code(setup):
    client, _ = setup
    student = await call(client, "GET", "/me", user=OUTSIDER)
    assert (student["role"], student["admin"], student["can_register"]) == ("student", False, True)
    teacher = await call(client, "GET", "/me", user=ADMIN)
    assert (teacher["role"], teacher["admin"], teacher["can_register"]) == ("teacher", True, False)
    assert CODE not in str(student) + str(teacher)


async def test_teacher_code_in_app_promotes_and_switches_the_chat_menu(context, setup, bot):
    client, dispatcher = setup
    sessions, *_ = context
    # An unfinished chat step of the student must not survive the role change.
    key = StorageKey(bot_id=bot.id, chat_id=OUTSIDER, user_id=OUTSIDER)
    await dispatcher.storage.set_state(key, TeacherRegistration.waiting_for_code)

    wrong = await call(
        client, "POST", "/teacher/register", user=OUTSIDER, json={"code": "nope"}, status=422
    )
    assert wrong["error"] == "Неверный код. Осталось попыток: 4."
    empty = await call(
        client, "POST", "/teacher/register", user=OUTSIDER, json={"code": "  "}, status=422
    )
    assert empty["error"] == "Введите код преподавателя."
    await call(client, "POST", "/teacher/register", user=OUTSIDER, json={"code": 1}, status=422)
    assert sent(bot, OUTSIDER) == []

    promoted = await call(
        client, "POST", "/teacher/register", user=OUTSIDER, json={"code": f" {CODE} "}
    )
    assert promoted["changed"] is True
    assert (promoted["me"]["role"], promoted["me"]["can_register"]) == ("teacher", False)
    [menu] = sent(bot, OUTSIDER)
    assert menu.text.startswith("Доступ преподавателя открыт.")
    assert isinstance(menu.reply_markup, ReplyKeyboardMarkup)
    rows = [[button.text for button in row] for row in menu.reply_markup.keyboard]
    assert rows == [[button.value for button in row] for row in MENU_LAYOUT[Role.TEACHER]]
    assert await dispatcher.storage.get_state(key) is None
    async with sessions() as session:
        user = await session.scalar(select(User).where(User.telegram_id == OUTSIDER))
        assert user.role == Role.TEACHER

    again = await call(client, "POST", "/teacher/register", user=OUTSIDER, json={"code": "x"})
    assert again["changed"] is False and len(sent(bot, OUTSIDER)) == 1


async def test_teacher_code_attempts_are_limited_and_code_can_be_disabled(context, setup, bot):
    client, _ = setup
    for remaining in (4, 3, 2, 1):
        body = await call(
            client, "POST", "/teacher/register", user=OUTSIDER, json={"code": "x"}, status=422
        )
        assert body["error"].endswith(f"{remaining}.")
    blocked = await call(
        client, "POST", "/teacher/register", user=OUTSIDER, json={"code": "x"}, status=429
    )
    assert blocked["error"].startswith("Слишком много неверных попыток")
    # The block also stops the right code, as in the chat.
    await call(client, "POST", "/teacher/register", user=OUTSIDER, json={"code": CODE}, status=429)

    sessions, *_ = context
    app = create_web_app(
        sessions,
        bot_token=SecretStr(TOKEN),
        auth_max_age=3600,
        bot=bot,
        notifier=Notifier(),
        storage=setup[1].storage,
    )
    async with TestClient(TestServer(app)) as plain:
        assert (await call(plain, "GET", "/me", user=STUDENT))["can_register"] is False
        disabled = await call(
            plain, "POST", "/teacher/register", user=STUDENT, json={"code": CODE}, status=409
        )
        assert disabled["error"].startswith("Регистрация по коду отключена")


async def test_only_administrators_grant_the_teacher_role(context, setup, bot):
    client, _ = setup
    sessions, *_ = context
    grant = "/admin/grant"
    denied = await call(
        client, "POST", grant, user=TEACHER, json={"telegram_id": str(OUTSIDER)}, status=403
    )
    assert denied["error"] == "Недостаточно прав для этого действия."
    for invalid in ("12a", "+404", "", "0", " "):
        await call(client, "POST", grant, user=ADMIN, json={"telegram_id": invalid}, status=422)
    await call(client, "POST", grant, user=ADMIN, json={"telegram_id": OUTSIDER}, status=422)
    unknown = await call(client, "POST", grant, user=ADMIN, json={"telegram_id": "999"}, status=409)
    assert "Попросите его сначала открыть бота" in unknown["error"]

    assert (await call(client, "POST", grant, user=ADMIN, json={"telegram_id": "404"}))["changed"]
    repeat = await call(client, "POST", grant, user=ADMIN, json={"telegram_id": " 404 "})
    assert repeat == {"changed": False}
    async with sessions() as session:
        user = await session.scalar(select(User).where(User.telegram_id == OUTSIDER))
        assert user.role == Role.TEACHER


async def test_notification_settings_match_the_chat_screen(context, setup):
    client, _ = setup
    sessions, ids, *_ = context
    shown = await call(client, "GET", "/notifications")
    assert [(item["key"], item["enabled"]) for item in shown["categories"]] == [
        ("submissions", True),
        ("joins", True),
        ("queue", True),
    ]
    assert shown["categories"][0]["label"] == "Новые решения на проверку"

    muted = await call(
        client, "PUT", "/notifications", json={"category": "submissions", "enabled": False}
    )
    assert [item["enabled"] for item in muted["categories"]] == [False, True, True]
    repeated = await call(
        client, "PUT", "/notifications", json={"category": "submissions", "enabled": False}
    )
    assert repeated == muted
    everything = await call(
        client, "PUT", "/notifications", json={"category": "all", "enabled": False}
    )
    assert [item["enabled"] for item in everything["categories"]] == [False, False, False]
    async with sessions() as session:
        preferences = await teacher_preferences(session, teacher_id=ids[TEACHER])
        assert (preferences.submissions, preferences.joins, preferences.queue) == (False,) * 3
    await call(client, "PUT", "/notifications", json={"category": "all", "enabled": True})

    for invalid in (
        {"category": "grades", "enabled": True},
        {"category": "queue", "enabled": "yes"},
        {"category": "queue"},
    ):
        await call(client, "PUT", "/notifications", json=invalid, status=422)
    student = await call(client, "GET", "/notifications", user=STUDENT, status=403)
    assert student["error"] == "Недостаточно прав для этого действия."
    await call(
        client,
        "PUT",
        "/notifications",
        user=STUDENT,
        json={"category": "all", "enabled": False},
        status=403,
    )


def test_screen_addresses_for_notification_buttons():
    assert mini_app_url(TUNNEL) == f"{TUNNEL}/app/"
    assert mini_app_url(TUNNEL, "/") == f"{TUNNEL}/app/"
    assert mini_app_url(TUNNEL, "/review/s/42") == f"{TUNNEL}/app/?screen=/review/s/42"
    assert mini_app_url(TUNNEL, "/groups/7") == f"{TUNNEL}/app/?screen=/groups/7"
    for invalid in ("", "review", "/review/s/4x", "/Review", "/a?b=1", "//x", "/x/1/2"):
        with pytest.raises(ValueError):
            mini_app_url(TUNNEL, invalid)


async def test_notices_open_the_same_object_in_the_app(context, setup, bot):
    client, dispatcher = setup
    sessions, _, group, assignment, _ = context
    task = await fresh_assignment(context)
    attempt = await call(
        client,
        "POST",
        f"/assignments/{task.id}/submissions",
        user=STUDENT,
        json={"text": "Работа", "expected_previous_id": None},
        status=201,
    )
    [received] = sent(bot, TEACHER)
    submission_id = attempt["submission"]["id"]
    assert labels(received) == [
        "Открыть попытку",
        "📱 Открыть в приложении",
        "🔕 Не присылать такие",
    ]
    assert app_buttons(received) == [f"{TUNNEL}/app/?screen=/review/s/{submission_id}"]

    await call(client, "POST", f"/submissions/{submission_id}/approve")
    assert app_buttons(sent(bot, STUDENT)[-1]) == [f"{TUNNEL}/app/?screen=/defense/a/{task.id}"]
    returned = await pending(context, task, student=203)
    await call(client, "POST", f"/submissions/{returned.id}/return", json={"comment": "Доработать"})
    assert app_buttons(sent(bot, 203)[-1]) == [f"{TUNNEL}/app/?screen=/tasks/a/{task.id}"]

    published = await call(
        client,
        "POST",
        f"/groups/{group.id}/assignments",
        json={"title": "ЛР 3", "description": "Условие", "format": "online"},
        headers={"Idempotency-Key": "publish-key-1"},
        status=201,
    )
    new_id = published["assignment"]["id"]
    assert app_buttons(sent(bot, 204)[-1]) == [f"{TUNNEL}/app/?screen=/tasks/a/{new_id}"]

    slot = await call(
        client,
        "POST",
        f"/assignments/{assignment.id}/slots",
        json={"date": "2026-11-12", "start": "10:00", "end": "12:00", "capacity": 3},
        headers={"Idempotency-Key": "slot-key-1"},
        status=201,
    )
    assert app_buttons(sent(bot, 202)[-1]) == [f"{TUNNEL}/app/?screen=/defense/a/{assignment.id}"]
    booked = await call(
        client,
        "POST",
        f"/assignments/{assignment.id}/enroll",
        user=STUDENT,
        json={"expected_entry_id": None, "expected_order_at": None, "slot_id": slot["slot"]["id"]},
    )
    assert booked["booking"]["slot"]["id"] == slot["slot"]["id"]
    assert app_buttons(sent(bot, TEACHER)[-1]) == [
        f"{TUNNEL}/app/?screen=/slots/s/{slot['slot']['id']}"
    ]

    # Joining happens in the chat; its notice links to the group card of the app.
    chat = create_dispatcher(sessions, notifier=Notifier(webapp_url=TUNNEL))
    async with sessions() as session:
        token = (await session.get(StudyGroup, group.id)).invite_token
    await chat.feed_update(bot, message_update(f"/start join_{token}", user_id=405))
    assert app_buttons(sent(bot, TEACHER)[-1]) == [f"{TUNNEL}/app/?screen=/groups/{group.id}"]


async def test_notices_have_no_app_button_without_mini_app(context, bot):
    sessions, _, group, *_ = context
    chat = create_dispatcher(sessions)
    async with sessions() as session:
        token = (await session.get(StudyGroup, group.id)).invite_token
    await chat.feed_update(bot, message_update(f"/start join_{token}", user_id=405))
    assert labels(sent(bot, TEACHER)[-1]) == ["Открыть группу", "🔕 Не присылать такие"]


@pytest.mark.parametrize("webapp_url", [TUNNEL, ""])
async def test_start_offers_the_app_in_its_own_message(context, bot, webapp_url):
    sessions, *_ = context
    chat = create_dispatcher(sessions, settings=settings(webapp_url=webapp_url))
    await chat.feed_update(bot, message_update("/start", user_id=STUDENT))
    messages = sent(bot, STUDENT)
    assert isinstance(messages[0].reply_markup, ReplyKeyboardMarkup)
    if webapp_url:
        assert len(messages) == 2
        assert app_buttons(messages[1]) == [f"{TUNNEL}/app/"]
        assert "PDF/ZIP" in messages[1].text
    else:
        assert len(messages) == 1


async def test_name_chosen_in_the_app_survives_telegram_and_reaches_the_teacher(context, setup):
    client, _ = setup
    sessions, ids, group, *_ = context
    renamed = await call(
        client, "PUT", "/me/name", user=STUDENT, json={"name": " Мария \n Иванова "}
    )
    assert (renamed["name"], renamed["custom_name"], renamed["telegram_name"]) == (
        "Мария Иванова",
        True,
        "User202",
    )
    # Every request refreshes the user from the signed Telegram data, the name stays.
    again = await call(client, "GET", "/me", user=STUDENT)
    assert (again["name"], again["custom_name"]) == ("Мария Иванова", True)
    for invalid in ({"name": ""}, {"name": "   "}, {"name": "x" * 129}, {"name": 5}, {}):
        error = await call(client, "PUT", "/me/name", user=STUDENT, json=invalid, status=422)
        assert error["error"].startswith("Имя должно содержать от 1 до 128 символов")

    async with sessions() as session:
        (await session.get(User, ids[203])).username = "student203"
        await session.commit()
    members = await call(client, "GET", f"/groups/{group.id}/members")
    assert members["group"] == {"id": group.id, "name": "ИВТ-101"}
    assert members["items"] == [
        {"name": "User203", "username": "student203"},
        {"name": "User204", "username": None},
        {"name": "Мария Иванова", "username": None},
    ]
    assert members["has_next"] is False

    restored = await call(client, "DELETE", "/me/name", user=STUDENT)
    assert (restored["name"], restored["custom_name"]) == ("User202", False)
    async with sessions() as session:
        assert (await session.get(User, ids[STUDENT])).full_name == "User202"


async def test_only_the_group_teacher_sees_its_students(context, setup):
    client, _ = setup
    sessions, ids, group, *_ = context
    path = f"/groups/{group.id}/members"
    assert (await call(client, "GET", path, user=STUDENT, status=403))["error"].startswith(
        "Недостаточно прав"
    )
    await call(client, "GET", path, user=ADMIN, status=404)
    await call(client, "GET", f"{path}?offset=x", status=422)
    async with sessions() as session:
        empty = await create_group(session, actor_id=ids[TEACHER], name="Пустая")
    listing = await call(client, "GET", f"/groups/{empty.id}/members")
    assert (listing["items"], listing["has_next"]) == ([], False)
