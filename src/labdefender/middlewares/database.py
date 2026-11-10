from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import TelegramObject
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from labdefender.services.users import ensure_user


class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Outer middleware runs before router filters. Do not register group/inline users.
        chat = data.get("event_chat")
        if chat is None or chat.type != ChatType.PRIVATE:
            return await handler(event, data)
        async with self.session_factory() as session:
            data["session"] = session
            telegram_user: TelegramUser | None = data.get("event_from_user")
            if telegram_user is not None:
                data["current_user"] = await ensure_user(
                    session,
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    full_name=telegram_user.full_name,
                )
            # A separate session per update. Uncommitted work is rolled back on close.
            return await handler(event, data)
