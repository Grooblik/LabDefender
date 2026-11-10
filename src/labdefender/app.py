import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from labdefender.config import Settings
from labdefender.db.session import create_database, initialize_database
from labdefender.handlers import common, student, teacher
from labdefender.middlewares.database import DatabaseMiddleware

logger = logging.getLogger(__name__)


def create_dispatcher(session_factory: async_sessionmaker[AsyncSession]) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    private = Router(name="private")
    # Ignore group messages and inline-mode callbacks in this MVP.
    private.message.filter(F.chat.type == ChatType.PRIVATE)
    private.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    private.message.outer_middleware(DatabaseMiddleware(session_factory))
    private.callback_query.outer_middleware(DatabaseMiddleware(session_factory))
    private.include_routers(
        common.create_router(), student.create_router(), teacher.create_router()
    )

    fallback = Router(name="fallback")

    @fallback.message()
    async def unknown_message(message: Message) -> None:
        await message.answer("Команда не найдена или недоступна для вашей роли. Помощь: /help")

    @fallback.callback_query()
    async def unknown_callback(callback: CallbackQuery) -> None:
        await callback.answer("Кнопка недоступна. Откройте меню заново через /start.")

    private.include_router(fallback)
    dispatcher.include_router(private)
    return dispatcher


async def run(settings: Settings) -> None:
    engine, session_factory = create_database(settings.database_url)
    try:
        await initialize_database(engine)
        dispatcher = create_dispatcher(session_factory)
        # Never log settings or the bot token. Bot owns and closes its HTTP session.
        async with Bot(token=settings.bot_token.get_secret_value()) as bot:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начать"),
                    BotCommand(command="help", description="Помощь"),
                    BotCommand(command="cancel", description="Отменить ввод"),
                ]
            )
            logger.info("Starting polling")
            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
    finally:
        await engine.dispose()
