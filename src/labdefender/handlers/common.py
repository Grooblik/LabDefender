from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.handlers import registration
from labdefender.keyboards.registration import role_keyboard


def create_router() -> Router:
    router = Router(name="common")

    @router.message(CommandStart())
    async def start(
        message: Message, command: CommandObject, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        if command.args:
            await message.answer("Приглашения пока недоступны. Запись в группу не выполнена.")
            return
        role = "преподаватель" if current_user.role == Role.TEACHER else "студент"
        await message.answer(
            f"Добро пожаловать в LabDefender!\nВаша роль: {role}.\nПомощь: /help",
            reply_markup=role_keyboard() if current_user.role == Role.STUDENT else None,
        )

    @router.message(Command("help"))
    async def help_command(message: Message, current_user: User, admin_ids: frozenset[int]) -> None:
        teacher_help = (
            "\n/teacher — получить доступ преподавателя"
            if current_user.role == Role.STUDENT
            else ""
        )
        admin_help = (
            "\n/grant_teacher <telegram_id> — выдать роль преподавателя"
            if current_user.telegram_id in admin_ids
            else ""
        )
        await message.answer(
            "LabDefender — сдача и защита лабораторных работ.\n\n"
            "/start — начать\n/help — помощь\n/cancel — отменить текущий ввод"
            f"{teacher_help}{admin_help}"
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Ввод отменён. Сохранённые данные не изменены.\nПомощь: /help")

    router.include_router(registration.create_router())
    return router
