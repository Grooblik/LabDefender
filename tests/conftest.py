from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, GetMe, SendDocument, SendMessage
from aiogram.types import Chat, Document, Message, User

from labdefender.db.session import create_database, initialize_database

TEST_TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


class RecordingSession(BaseSession):
    """Telegram transport stub: no network calls, actual Dispatcher routing."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, SendDocument):
            assert isinstance(method.document, str), (
                "Only existing Telegram file_id values are sent"
            )
            return Message(
                message_id=len(self.calls),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                document=Document(file_id=method.document, file_unique_id="unique-document"),
            )
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        if isinstance(method, GetMe):
            return User(
                id=bot.id, is_bot=True, first_name="LabDefender", username="LabDefenderTestBot"
            )
        raise AssertionError(f"Unexpected Telegram method: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs) -> AsyncGenerator[bytes, None]:
        raise AssertionError("Files must not be downloaded")
        yield b""  # pragma: no cover


@pytest.fixture
async def database(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
    await initialize_database(engine)
    try:
        yield engine, sessions
    finally:
        await engine.dispose()


@pytest.fixture
async def bot():
    async with Bot(token=TEST_TOKEN, session=RecordingSession()) as instance:
        yield instance
