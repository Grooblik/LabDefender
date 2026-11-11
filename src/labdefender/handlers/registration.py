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
from labdefender.filters.plain_text import PlainTextInput
from labdefender.keyboards.menu import MenuCallback, cancel_keyboard, main_menu
from labdefender.services.auth import grant_teacher, register_teacher
from labdefender.services.errors import AccessDenied, RegistrationBlocked, RegistrationDisabled
from labdefender.states.registration import TeacherGrant, TeacherRegistration

TEACHER_MENU_HINT = "Разделы преподавателя — на кнопках под полем ввода."


async def request_teacher_role(
    message: Message, current_user: User, state: FSMContext, registration_code: SecretStr
) -> None:
    await state.clear()
    if current_user.role == Role.TEACHER:
        await message.answer(
            f"У вас уже есть доступ преподавателя. {TEACHER_MENU_HINT}",
            reply_markup=main_menu(Role.TEACHER),
        )
        return
    if not registration_code.get_secret_value():
        raise RegistrationDisabled()
    await state.set_state(TeacherRegistration.waiting_for_code)
    await message.answer("Введите код преподавателя.", reply_markup=cancel_keyboard())


def parse_telegram_id(text: str | None) -> int | None:
    try:
        value = int((text or "").strip())
    except ValueError:
        return None
    return value if 0 < value < 2**63 else None


async def start_grant(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TeacherGrant.waiting_for_id)
    await message.answer(
        "Отправьте Telegram ID пользователя, которому нужна роль преподавателя. "
        "Он узнает свой ID кнопкой «🆔 Мой Telegram ID» в «❓ Помощь» или командой /id.",
        reply_markup=cancel_keyboard(),
    )


async def grant_and_report(
    message: Message,
    session: AsyncSession,
    current_user: User,
    admin_ids: frozenset[int],
    target_id: int,
) -> None:
    changed = await grant_teacher(
        session, actor_id=current_user.id, target_telegram_id=target_id, admin_ids=admin_ids
    )
    await message.answer(
        "Роль преподавателя выдана." if changed else "У пользователя уже есть роль преподавателя."
    )


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
        state: FSMContext,
        admin_ids: frozenset[int],
    ) -> None:
        if current_user.telegram_id not in admin_ids:
            raise AccessDenied()
        if not command.args:
            await start_grant(message, state)
            return
        target_id = parse_telegram_id(command.args)
        if target_id is None:
            await message.answer("Формат команды: /grant_teacher <telegram_id>")
            return
        await state.clear()
        await grant_and_report(message, session, current_user, admin_ids, target_id)

    @router.callback_query(MenuCallback.filter(F.action == "grant"))
    async def grant_button(
        callback: CallbackQuery, current_user: User, state: FSMContext, admin_ids: frozenset[int]
    ) -> None:
        if current_user.telegram_id not in admin_ids:
            raise AccessDenied()
        if isinstance(callback.message, Message):
            await start_grant(callback.message, state)
        await callback.answer()

    @router.message(TeacherGrant.waiting_for_id, PlainTextInput())
    async def grant_id_message(
        message: Message,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        admin_ids: frozenset[int],
    ) -> None:
        target_id = parse_telegram_id(message.text)
        if target_id is None:
            await message.answer(
                "Нужен числовой Telegram ID. Отправьте его ещё раз или нажмите «Отмена».",
                reply_markup=cancel_keyboard(),
            )
            return
        # A domain error (unknown user) keeps the step: another ID can be sent.
        await grant_and_report(message, session, current_user, admin_ids, target_id)
        await state.clear()

    @router.message(TeacherGrant.waiting_for_id, ~F.text)
    async def grant_requires_text(message: Message) -> None:
        await message.answer(
            "Отправьте Telegram ID текстом или нажмите «Отмена».", reply_markup=cancel_keyboard()
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

    # Role buttons from the /start message of earlier versions may remain in chats.
    @router.callback_query(F.data == "auth:student")
    async def student_button(
        callback: CallbackQuery, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        if isinstance(callback.message, Message):
            text = (
                f"У вас роль преподавателя. {TEACHER_MENU_HINT}"
                if current_user.role == Role.TEACHER
                else "Вы вошли как студент. Разделы — на кнопках под полем ввода."
            )
            await callback.message.answer(text, reply_markup=main_menu(current_user.role))
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
            f"Доступ преподавателя открыт. {TEACHER_MENU_HINT}"
            if changed
            else f"У вас уже есть доступ преподавателя. {TEACHER_MENU_HINT}",
            reply_markup=main_menu(Role.TEACHER),
        )

    @router.message(TeacherRegistration.waiting_for_code, ~F.text)
    async def registration_requires_text(message: Message) -> None:
        await message.answer(
            "Отправьте код текстовым сообщением или нажмите «Отмена».",
            reply_markup=cancel_keyboard(),
        )

    return router
