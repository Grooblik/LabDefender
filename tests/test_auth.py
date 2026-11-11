import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendMessage
from aiogram.types import Update
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from labdefender.__main__ import main
from labdefender.app import create_dispatcher
from labdefender.config import Settings
from labdefender.db.models import TeacherRegistrationAttempt, User
from labdefender.db.session import create_database, initialize_database
from labdefender.enums import Role
from labdefender.services.auth import grant_teacher, register_teacher
from labdefender.services.errors import (
    AccessDenied,
    DomainError,
    InvalidRegistrationCode,
    RegistrationBlocked,
    RegistrationDisabled,
    UserNotRegistered,
)
from labdefender.services.users import ensure_user

TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
CODE = "код-для-преподавателя"


def message_update(text, *, user_id=101):
    message = {
        "message_id": 10,
        "date": datetime.now(UTC),
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.model_validate({"update_id": 1, "message": message})


def callback_update(data, *, user_id=101):
    message = message_update("Menu", user_id=user_id).message.model_dump(by_alias=True)
    message["from"] = {"id": 123456, "is_bot": True, "first_name": "Bot"}
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "auth-callback",
                "chat_instance": "test-chat",
                "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "message": message,
            },
        }
    )


def settings(**kwargs):
    return Settings(
        bot_token=TOKEN,
        teacher_registration_code=CODE,
        admin_ids=frozenset({900}),
        _env_file=None,
        **kwargs,
    )


def texts(bot):
    return [call.text for call in bot.session.calls if isinstance(call, SendMessage)]


@pytest.fixture
async def registered(database):
    _, sessions = database
    async with sessions() as session:
        user = await ensure_user(session, telegram_id=101, username=None, full_name="Test")
        return sessions, user.id


async def test_registration_success_and_idempotence(registered):
    sessions, user_id = registered
    async with sessions() as session:
        with pytest.raises(InvalidRegistrationCode):
            await register_teacher(
                session, actor_id=user_id, code="wrong", registration_code=SecretStr(CODE)
            )
    async with sessions() as session:
        assert await register_teacher(
            session, actor_id=user_id, code=CODE, registration_code=SecretStr(CODE)
        )
        assert (await session.get(User, user_id)).role == Role.TEACHER
        assert await session.get(TeacherRegistrationAttempt, user_id) is None
    async with sessions() as session:
        assert not await register_teacher(
            session, actor_id=user_id, code="", registration_code=SecretStr("")
        )


async def test_disabled_registration_cannot_grant_role(registered):
    sessions, user_id = registered
    async with sessions() as session:
        with pytest.raises(RegistrationDisabled):
            await register_teacher(
                session, actor_id=user_id, code="anything", registration_code=SecretStr("")
            )
    async with sessions() as session:
        assert (await session.get(User, user_id)).role == Role.STUDENT
        assert await session.get(TeacherRegistrationAttempt, user_id) is None


async def test_lockout_survives_reopen_and_blocks_correct_code(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'lockout.sqlite3'}"
    engine, sessions = create_database(url)
    try:
        await initialize_database(engine)
        async with sessions() as session:
            user_id = (
                await ensure_user(session, telegram_id=101, username=None, full_name="Test")
            ).id
        for attempt in range(5):
            async with sessions() as session:
                with pytest.raises(
                    RegistrationBlocked if attempt == 4 else InvalidRegistrationCode
                ):
                    await register_teacher(
                        session, actor_id=user_id, code="wrong", registration_code=SecretStr(CODE)
                    )
    finally:
        await engine.dispose()

    engine, sessions = create_database(url)
    try:
        await initialize_database(engine)
        async with sessions() as session:
            with pytest.raises(RegistrationBlocked):
                await register_teacher(
                    session, actor_id=user_id, code=CODE, registration_code=SecretStr(CODE)
                )
        async with sessions() as session:
            assert (await session.get(User, user_id)).role == Role.STUDENT
            assert (await session.get(TeacherRegistrationAttempt, user_id)).failed_attempts == 5
    finally:
        await engine.dispose()


async def test_parallel_failures_cannot_bypass_attempt_limit(registered):
    sessions, user_id = registered

    async def attempt():
        async with sessions() as session:
            try:
                await register_teacher(
                    session, actor_id=user_id, code="wrong", registration_code=SecretStr(CODE)
                )
            except DomainError as error:
                return error

    errors = await asyncio.gather(*(attempt() for _ in range(8)))
    assert sum(isinstance(error, InvalidRegistrationCode) for error in errors) == 4
    assert sum(isinstance(error, RegistrationBlocked) for error in errors) == 4
    async with sessions() as session:
        assert (await session.get(TeacherRegistrationAttempt, user_id)).failed_attempts == 5


async def test_expired_lockout_allows_correct_code(registered):
    sessions, user_id = registered
    async with sessions() as session:
        session.add(
            TeacherRegistrationAttempt(
                user_id=user_id,
                failed_attempts=5,
                window_started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=11),
            )
        )
        await session.commit()
    async with sessions() as session:
        assert await register_teacher(
            session, actor_id=user_id, code=CODE, registration_code=SecretStr(CODE)
        )


async def test_grant_requires_admin_telegram_id_and_is_idempotent(registered):
    sessions, user_id = registered
    async with sessions() as session:
        admin = await ensure_user(session, telegram_id=900, username=None, full_name="Admin")
        admin_id = admin.id
    async with sessions() as session:
        with pytest.raises(AccessDenied):
            await grant_teacher(
                session, actor_id=user_id, target_telegram_id=101, admin_ids=frozenset({900})
            )
    async with sessions() as session:
        assert await grant_teacher(
            session, actor_id=admin_id, target_telegram_id=101, admin_ids=frozenset({900})
        )
    async with sessions() as session:
        assert not await grant_teacher(
            session, actor_id=admin_id, target_telegram_id=101, admin_ids=frozenset({900})
        )
    async with sessions() as session:
        # Being a teacher still does not confer administrator rights.
        with pytest.raises(AccessDenied):
            await grant_teacher(
                session, actor_id=user_id, target_telegram_id=900, admin_ids=frozenset({900})
            )


async def test_grant_unknown_user_does_not_create_account(registered):
    sessions, user_id = registered
    async with sessions() as session:
        with pytest.raises(UserNotRegistered):
            await grant_teacher(
                session, actor_id=user_id, target_telegram_id=404, admin_ids=frozenset({101})
            )
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_additive_schema_update_preserves_existing_users(database):
    engine, sessions = database
    async with sessions() as session:
        await ensure_user(session, telegram_id=101, username=None, full_name="Existing")
    async with engine.begin() as connection:
        await connection.run_sync(TeacherRegistrationAttempt.__table__.drop)
    await initialize_database(engine)
    async with sessions() as session:
        assert (await session.scalar(select(User))).full_name == "Existing"
        assert (
            await session.scalar(select(func.count()).select_from(TeacherRegistrationAttempt)) == 0
        )


async def test_teacher_dialogue_deletes_code_and_does_not_store_or_echo_it(database, bot, caplog):
    _, sessions = database
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("/start"))
    assert "«🎓 Я преподаватель»" in texts(bot)[-1]
    await dispatcher.feed_update(bot, message_update("❓ Помощь"))
    buttons = {
        button.text: button.callback_data
        for row in bot.session.calls[-1].reply_markup.inline_keyboard
        for button in row
    }
    assert buttons["🎓 Я преподаватель"] == "auth:teacher"
    await dispatcher.feed_update(bot, callback_update("auth:teacher"))
    await dispatcher.feed_update(bot, message_update(CODE))
    assert any(isinstance(call, DeleteMessage) for call in bot.session.calls)
    assert "Доступ преподавателя открыт" in texts(bot)[-1]
    key = StorageKey(bot_id=bot.id, chat_id=101, user_id=101)
    assert await dispatcher.storage.get_state(key) is None
    assert await dispatcher.storage.get_data(key) == {}
    assert CODE not in " ".join(texts(bot))
    assert CODE not in caplog.text
    await dispatcher.feed_update(bot, message_update("/start"))
    assert "Ваша роль: преподаватель" in texts(bot)[-1]


async def test_wrong_code_can_be_retried_and_domain_error_is_handled(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("/teacher"))
    await dispatcher.feed_update(bot, message_update("wrong"))
    assert "Осталось попыток: 4" in texts(bot)[-1]
    await dispatcher.feed_update(bot, message_update(CODE))
    assert "Доступ преподавателя открыт" in texts(bot)[-1]


@pytest.mark.parametrize("command", ["/cancel", "/start"])
async def test_cancelled_dialogue_cannot_promote_user(database, bot, command):
    _, sessions = database
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("/teacher"))
    await dispatcher.feed_update(bot, message_update(command))
    await dispatcher.feed_update(bot, message_update(CODE))
    async with sessions() as session:
        assert (await session.scalar(select(User))).role == Role.STUDENT
        assert (
            await session.scalar(select(func.count()).select_from(TeacherRegistrationAttempt)) == 0
        )


async def test_disabled_registration_button_answers_with_alert(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, callback_update("auth:teacher"))
    response = bot.session.calls[-1]
    assert isinstance(response, AnswerCallbackQuery)
    assert response.show_alert is True
    assert "отключена" in response.text


async def test_student_button_does_not_remove_teacher_role(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("/teacher"))
    await dispatcher.feed_update(bot, message_update(CODE))
    await dispatcher.feed_update(bot, callback_update("auth:student"))
    async with sessions() as session:
        assert (await session.scalar(select(User))).role == Role.TEACHER


async def test_delete_failure_does_not_prevent_registration(database, bot, monkeypatch):
    _, sessions = database
    original = bot.session.make_request

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, DeleteMessage):
            raise TelegramBadRequest(method=method, message="Message cannot be deleted")
        return await original(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", request)
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("/teacher"))
    await dispatcher.feed_update(bot, message_update(CODE))
    assert "Доступ преподавателя открыт" in texts(bot)[-1]


async def test_admin_command_and_help_are_restricted(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("/start"))
    await dispatcher.feed_update(bot, message_update("/grant_teacher 101"))
    assert "Недостаточно прав" in texts(bot)[-1]
    await dispatcher.feed_update(bot, message_update("/help"))
    assert "/grant_teacher" not in texts(bot)[-1]
    await dispatcher.feed_update(bot, message_update("/help", user_id=900))
    assert "/grant_teacher" in texts(bot)[-1]
    await dispatcher.feed_update(bot, message_update("/grant_teacher 101", user_id=900))
    assert texts(bot)[-1] == "Роль преподавателя выдана."
    await dispatcher.feed_update(bot, message_update("/grant_teacher invalid", user_id=900))
    assert "Формат команды" in texts(bot)[-1]
    await dispatcher.feed_update(bot, message_update("/start"))
    assert "Ваша роль: преподаватель" in texts(bot)[-1]


def test_auth_settings_use_json_ids_and_keep_code_secret(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TEACHER_REGISTRATION_CODE", f" {CODE} ")
    monkeypatch.setenv("ADMIN_IDS", "[900,901]")
    configured = Settings(_env_file=None)
    assert configured.admin_ids == frozenset({900, 901})
    assert configured.teacher_registration_code.get_secret_value() == CODE
    assert CODE not in repr(configured)
    with pytest.raises(ValidationError):
        Settings(admin_ids=frozenset({-1}), _env_file=None)


async def test_expired_window_starts_new_attempt_count(registered):
    sessions, user_id = registered
    old_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=11)
    async with sessions() as session:
        session.add(
            TeacherRegistrationAttempt(
                user_id=user_id, failed_attempts=5, window_started_at=old_time
            )
        )
        await session.commit()
    async with sessions() as session:
        with pytest.raises(InvalidRegistrationCode) as error:
            await register_teacher(
                session, actor_id=user_id, code="wrong", registration_code=SecretStr(CODE)
            )
        assert error.value.remaining_attempts == 4
        attempt = await session.get(TeacherRegistrationAttempt, user_id)
        assert attempt.failed_attempts == 1
        assert attempt.window_started_at > old_time


async def test_manual_grant_works_during_registration_lockout(registered):
    sessions, user_id = registered
    async with sessions() as session:
        admin_id = (
            await ensure_user(session, telegram_id=900, username=None, full_name="Admin")
        ).id
        session.add(
            TeacherRegistrationAttempt(
                user_id=user_id,
                failed_attempts=5,
                window_started_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await session.commit()
    async with sessions() as session:
        assert await grant_teacher(
            session, actor_id=admin_id, target_telegram_id=101, admin_ids=frozenset({900})
        )
        assert (await session.get(User, user_id)).role == Role.TEACHER
        assert await session.get(TeacherRegistrationAttempt, user_id) is None


async def test_role_button_alone_does_not_grant_teacher_and_restart_drops_draft(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, callback_update("auth:teacher"))
    async with sessions() as session:
        assert (await session.scalar(select(User))).role == Role.STUDENT
    # FSM is intentionally ephemeral; sending a code after restart requires /teacher again.
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update(CODE))
    async with sessions() as session:
        assert (await session.scalar(select(User))).role == Role.STUDENT


async def test_commands_do_not_count_as_codes_and_cancel_does_not_reset_lockout(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("/teacher"))
    await dispatcher.feed_update(bot, message_update("/help"))
    assert not any(isinstance(call, DeleteMessage) for call in bot.session.calls)
    for _ in range(5):
        await dispatcher.feed_update(bot, message_update("wrong"))
    assert "Слишком много неверных попыток" in texts(bot)[-1]
    await dispatcher.feed_update(bot, message_update("/cancel"))
    await dispatcher.feed_update(bot, message_update("/start"))
    await dispatcher.feed_update(bot, message_update("/teacher"))
    await dispatcher.feed_update(bot, message_update(CODE))
    assert "Слишком много неверных попыток" in texts(bot)[-1]
    async with sessions() as session:
        assert (await session.scalar(select(User))).role == Role.STUDENT


def test_malformed_admin_settings_stop_startup_without_exposing_input(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ADMIN_IDS", CODE)
    with pytest.raises(SystemExit) as error:
        main()
    assert "ADMIN_IDS" in str(error.value)
    assert CODE not in str(error.value)


@pytest.mark.parametrize("code", ["/unusable-code", "a" * 4097])
def test_config_rejects_codes_that_cannot_be_entered(code):
    with pytest.raises(ValidationError):
        Settings(bot_token=TOKEN, teacher_registration_code=code, _env_file=None)
