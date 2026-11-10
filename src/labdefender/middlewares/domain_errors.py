from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from labdefender.services.errors import DomainError


class DomainErrorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except DomainError as error:
            if isinstance(event, CallbackQuery):
                await event.answer(str(error), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(str(error))
            else:
                raise
