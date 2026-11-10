from aiogram.enums import MessageEntityType
from aiogram.filters import Filter
from aiogram.types import Message


class PlainTextInput(Filter):
    """Accept text including // comments, while leaving Telegram commands to command handlers."""

    async def __call__(self, message: Message) -> bool:
        return message.text is not None and not any(
            entity.type == MessageEntityType.BOT_COMMAND and entity.offset == 0
            for entity in message.entities or ()
        )
