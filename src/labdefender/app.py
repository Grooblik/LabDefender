import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, CallbackQuery, Message
from pydantic import SecretStr
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from labdefender.config import Settings
from labdefender.db.session import create_database, initialize_database
from labdefender.handlers import common, student, teacher
from labdefender.local import database_path, instance_locks
from labdefender.middlewares.database import DatabaseMiddleware
from labdefender.middlewares.domain_errors import DomainErrorMiddleware

logger = logging.getLogger(__name__)


def create_dispatcher(
    session_factory: async_sessionmaker[AsyncSession], *, settings: Settings | None = None
) -> Dispatcher:
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
        registration_code=settings.teacher_registration_code if settings else SecretStr(""),
        admin_ids=settings.admin_ids if settings else frozenset(),
    )
    private = Router(name="private")
    # Ignore group messages and inline-mode callbacks in this MVP.
    private.message.filter(F.chat.type == ChatType.PRIVATE)
    private.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    private.message.outer_middleware(DatabaseMiddleware(session_factory))
    private.callback_query.outer_middleware(DatabaseMiddleware(session_factory))
    private.message.outer_middleware(DomainErrorMiddleware())
    private.callback_query.outer_middleware(DomainErrorMiddleware())
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
    path = database_path(settings.database_url)
    bot_id = int(settings.bot_token.get_secret_value().split(":", 1)[0])
    with instance_locks(path, bot_id=bot_id):
        await _run(settings, path)


async def _run(settings: Settings, path: Path) -> None:
    engine, session_factory = create_database(
        str(URL.create("sqlite+aiosqlite", database=str(path)))
    )
    try:
        await initialize_database(engine)
        dispatcher = create_dispatcher(session_factory, settings=settings)
        # Never log settings or the bot token. Bot owns and closes its HTTP session.
        async with Bot(token=settings.bot_token.get_secret_value()) as bot:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начать"),
                    BotCommand(command="help", description="Помощь"),
                    BotCommand(command="id", description="Мой Telegram ID"),
                    BotCommand(command="cancel", description="Отменить ввод"),
                    BotCommand(command="teacher", description="Доступ преподавателя"),
                    BotCommand(command="my_groups", description="Мои группы"),
                    BotCommand(command="create_group", description="Создать группу"),
                    BotCommand(command="invite", description="Пригласить студентов"),
                    BotCommand(command="new_assignment", description="Новое задание"),
                    BotCommand(command="submissions", description="Проверить решения"),
                    BotCommand(command="assignments", description="Задания студента"),
                    BotCommand(command="new_slot", description="Открыть слот защиты"),
                    BotCommand(command="queue", description="Защита и очередь"),
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
