from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.services.auth import grant_teacher, register_teacher
from labdefender.services.errors import AccessDenied, RegistrationBlocked, RegistrationDisabled
from labdefender.states.registration import TeacherRegistration


async def request_teacher_role(
    message: Message, current_user: User, state: FSMContext, registration_code: SecretStr
) -> None:
    await state.clear()
    if current_user.role == Role.TEACHER:
        await message.answer("У вас уже есть доступ преподавателя. Помощь: /help")
        return
    if not registration_code.get_secret_value():
        raise RegistrationDisabled()
    await state.set_state(TeacherRegistration.waiting_for_code)
    await message.answer("Введите код преподавателя. Для отмены отправьте /cancel.")


def create_router() -> Router:
    router = Router(name="registration")

    @router.message(Command("teacher"))
    async def teacher_command(
        message: Message, current_user: User, state: FSMContext, registration_code: SecretStr
    ) -> None:
        await request_teacher_role(message, current_user, state, registration_code)

    @router.message(Command("grant_teacher"))
    async def grant_command(
        message: Message,
        command: CommandObject,
        session: AsyncSession,
        current_user: User,
        admin_ids: frozenset[int],
    ) -> None:
        if current_user.telegram_id not in admin_ids:
            raise AccessDenied()
        try:
            target_id = int(command.args or "")
        except ValueError:
            target_id = 0
        if not 0 < target_id < 2**63:
            await message.answer("Формат команды: /grant_teacher <telegram_id>")
            return
        changed = await grant_teacher(
            session, actor_id=current_user.id, target_telegram_id=target_id, admin_ids=admin_ids
        )
        await message.answer(
            "Роль преподавателя выдана."
            if changed
            else "У пользователя уже есть роль преподавателя."
        )

    @router.callback_query(F.data == "auth:teacher")
    async def teacher_button(
        callback: CallbackQuery,
        current_user: User,
        state: FSMContext,
        registration_code: SecretStr,
    ) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Откройте меню заново через /start.")
            return
        # Answer after the operation so domain errors can send a single alert.
        await request_teacher_role(callback.message, current_user, state, registration_code)
        await callback.answer()

    @router.callback_query(F.data == "auth:student")
    async def student_button(
        callback: CallbackQuery, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        if isinstance(callback.message, Message):
            text = (
                "У вас роль преподавателя. Помощь: /help"
                if current_user.role == Role.TEACHER
                else "Вы вошли как студент. Помощь: /help"
            )
            await callback.message.answer(text)
        await callback.answer()

    @router.message(TeacherRegistration.waiting_for_code, F.text, ~F.text.startswith("/"))
    async def registration_code_message(
        message: Message,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        registration_code: SecretStr,
    ) -> None:
        with suppress(TelegramAPIError):
            await message.delete()
        try:
            changed = await register_teacher(
                session,
                actor_id=current_user.id,
                code=message.text,
                registration_code=registration_code,
            )
        except (RegistrationBlocked, RegistrationDisabled):
            await state.clear()
            raise
        await state.clear()
        await message.answer(
            "Доступ преподавателя открыт. Помощь: /help"
            if changed
            else "У вас уже есть доступ преподавателя. Помощь: /help"
        )

    @router.message(TeacherRegistration.waiting_for_code, ~F.text)
    async def registration_requires_text(message: Message) -> None:
        await message.answer("Отправьте код текстовым сообщением или отмените ввод: /cancel.")

    return router
