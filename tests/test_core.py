import asyncio
from datetime import UTC, datetime

import pytest
from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Update
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from labdefender.app import create_dispatcher
from labdefender.config import Settings
from labdefender.db.models import User
from labdefender.db.session import create_database, initialize_database
from labdefender.enums import Role
from labdefender.services.users import ensure_user

TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def message_update(message_text, *, chat_type="private", user_id=101):
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": datetime.now(UTC),
                "chat": {"id": user_id, "type": chat_type},
                "from": {"id": user_id, "is_bot": False, "first_name": "Студент"},
                "text": message_text,
                "entities": [
                    {"type": "bot_command", "offset": 0, "length": len(message_text.split()[0])}
                ],
            },
        }
    )


def callback_update():
    message = message_update("card").message.model_dump()
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "callback-1",
                "from": message["from_user"],
                "chat_instance": "test-chat",
                "data": "teacher:test",
                "message": message,
            },
        }
    )


def test_config_keeps_token_secret_and_rejects_invalid_values(monkeypatch):
    for variable in (
        "BOT_TOKEN",
        "DATABASE_URL",
        "LOG_LEVEL",
        "TEACHER_REGISTRATION_CODE",
        "ADMIN_IDS",
    ):
        monkeypatch.delenv(variable, raising=False)
    settings = Settings(bot_token=TOKEN, _env_file=None)
    assert TOKEN not in repr(settings)
    with pytest.raises(ValidationError):
        Settings(bot_token="", _env_file=None)
    with pytest.raises(ValidationError):
        Settings(bot_token=TOKEN, database_url="sqlite:///test.db", _env_file=None)
    with pytest.raises(ValidationError):
        Settings(bot_token=TOKEN, database_url="not a url", _env_file=None)


async def test_identity_refresh_preserves_teacher_role(database):
    _, sessions = database
    async with sessions() as session:
        user = await ensure_user(session, telegram_id=101, username="old", full_name="Old")
        user.role = Role.TEACHER
        await session.commit()
    async with sessions() as session:
        user = await ensure_user(session, telegram_id=101, username=None, full_name="New")
        assert user.role == Role.TEACHER
        assert user.username is None
        assert user.full_name == "New"
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_concurrent_identity_creation_is_idempotent(database):
    _, sessions = database

    async def register():
        async with sessions() as session:
            return (
                await ensure_user(session, telegram_id=101, username=None, full_name="Студент")
            ).id

    ids = await asyncio.gather(*(register() for _ in range(5)))
    assert len(set(ids)) == 1


async def test_users_survive_database_reopen(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'nested' / 'persistent.sqlite3'}"
    engine, sessions = create_database(url)
    try:
        await initialize_database(engine)
        async with sessions() as session:
            await ensure_user(session, telegram_id=101, username=None, full_name="Студент")
    finally:
        await engine.dispose()
    engine, sessions = create_database(url)
    try:
        await initialize_database(engine)
        async with sessions() as session:
            assert (await session.scalar(select(User))).telegram_id == 101
    finally:
        await engine.dispose()


async def test_schema_and_foreign_keys(database):
    engine, sessions = database
    async with engine.connect() as connection:
        tables = (
            (await connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'")))
            .scalars()
            .all()
        )
        assert set(tables) == {
            "users",
            "study_groups",
            "memberships",
            "assignments",
            "submissions",
            "defense_slots",
            "queue_entries",
            "teacher_registration_attempts",
        }
        assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
    async with sessions() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("INSERT INTO memberships (group_id, student_id) VALUES (999, 999)")
            )
        await session.rollback()


async def test_start_registers_once_and_answers_in_russian(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions)
    for _ in range(2):
        await dispatcher.feed_update(bot, message_update("/start"))
    assert len(bot.session.calls) == 2
    assert all(isinstance(call, SendMessage) for call in bot.session.calls)
    assert "Ваша роль: студент" in bot.session.calls[-1].text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_id_command_returns_callers_telegram_id(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/id", user_id=99887766))
    assert bot.session.calls[-1].text == "Ваш Telegram ID: 99887766"


@pytest.mark.parametrize("command", ["/cancel", "/start"])
async def test_common_commands_clear_dialogue(database, bot, command):
    _, sessions = database
    dispatcher = create_dispatcher(sessions)
    key = StorageKey(bot_id=bot.id, chat_id=101, user_id=101)
    await dispatcher.storage.set_state(key, "example:waiting")
    await dispatcher.storage.set_data(key, {"draft": "temporary"})
    await dispatcher.feed_update(bot, message_update(command))
    assert await dispatcher.storage.get_state(key) is None
    assert await dispatcher.storage.get_data(key) == {}


async def test_invalid_invite_is_not_reported_as_joined(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/start join_test"))
    assert "Запись в группу не выполнена" in bot.session.calls[-1].text


async def test_group_messages_are_ignored(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/start", chat_type="group"))
    assert bot.session.calls == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.parametrize("is_teacher", [False, True])
async def test_teacher_router_checks_messages_and_callbacks(database, bot, is_teacher):
    _, sessions = database
    async with sessions() as session:
        user = await ensure_user(session, telegram_id=101, username=None, full_name="Test")
        if is_teacher:
            user.role = Role.TEACHER
            await session.commit()

    dispatcher = create_dispatcher(sessions)
    teacher_router = dispatcher.sub_routers[0].sub_routers[2]

    @teacher_router.message(Command("teacher_test"))
    async def teacher_message(message, session, current_user):
        assert current_user.role == Role.TEACHER
        assert session is not None
        await message.answer("Teacher allowed")

    @teacher_router.callback_query(F.data == "teacher:test")
    async def teacher_callback(callback, current_user):
        assert current_user.role == Role.TEACHER
        await callback.answer("Teacher allowed")

    await dispatcher.feed_update(bot, message_update("/teacher_test"))
    await dispatcher.feed_update(bot, callback_update())
    assert isinstance(bot.session.calls[-1], AnswerCallbackQuery)
    assert (bot.session.calls[0].text == "Teacher allowed") is is_teacher
    assert (bot.session.calls[-1].text == "Teacher allowed") is is_teacher


async def test_uncommitted_handler_changes_roll_back(database, bot):
    _, sessions = database
    dispatcher = create_dispatcher(sessions)
    student_router = dispatcher.sub_routers[0].sub_routers[1]

    @student_router.message(Command("failing_test"))
    async def failing_handler(message, session, current_user):
        current_user.full_name = "Must roll back"
        await session.flush()
        raise RuntimeError("Example failure")

    with pytest.raises(RuntimeError, match="Example failure"):
        await dispatcher.feed_update(bot, message_update("/failing_test"))
    async with sessions() as session:
        assert (await session.scalar(select(User))).full_name == "Студент"
