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
from labdefender.keyboards.menu import MenuCallback, cancel_keyboard, main_menu, name_keyboard
from labdefender.services.auth import grant_teacher, register_teacher
from labdefender.services.errors import AccessDenied, RegistrationBlocked, RegistrationDisabled
from labdefender.services.users import (
    DISPLAY_NAME_LIMIT,
    has_chosen_name,
    reset_display_name,
    set_display_name,
)
from labdefender.states.registration import DisplayName, TeacherGrant, TeacherRegistration

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


async def request_display_name(
    message: Message, session: AsyncSession, current_user: User, state: FSMContext
) -> None:
    await state.clear()
    await state.set_state(DisplayName.waiting_for_name)
    await message.answer(
        f"Сейчас вас видят как «{current_user.full_name}». Отправьте имя и фамилию так, "
        f"как их должны видеть преподаватель и студенты (до {DISPLAY_NAME_LIMIT} символов). "
        "Username из Telegram остаётся рядом с именем.",
        reply_markup=name_keyboard(chosen=await has_chosen_name(session, user_id=current_user.id)),
    )


def create_router() -> Router:
    router = Router(name="registration")

    @router.message(Command("name"))
    async def name_command(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await request_display_name(message, session, current_user, state)

    @router.callback_query(MenuCallback.filter(F.action == "name"))
    async def name_button(
        callback: CallbackQuery, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        if isinstance(callback.message, Message):
            await request_display_name(callback.message, session, current_user, state)
        await callback.answer()

    @router.message(DisplayName.waiting_for_name, PlainTextInput())
    async def name_message(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        # An invalid name keeps the step: the domain error asks for it again.
        user = await set_display_name(session, actor_id=current_user.id, name=message.text)
        await state.clear()
        await message.answer(
            f"Имя сохранено: «{user.full_name}». Так вас видят в списках, очереди и уведомлениях.",
            reply_markup=main_menu(user.role),
        )

    @router.message(DisplayName.waiting_for_name, ~F.text)
    async def name_requires_text(message: Message) -> None:
        await message.answer(
            "Отправьте имя текстом или нажмите «Отмена».", reply_markup=cancel_keyboard()
        )

    @router.callback_query(MenuCallback.filter(F.action == "name_reset"))
    async def name_reset(
        callback: CallbackQuery, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        user = await reset_display_name(
            session, actor_id=current_user.id, telegram_name=callback.from_user.full_name
        )
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(
                f"Имя из Telegram восстановлено: «{user.full_name}».",
                reply_markup=main_menu(user.role),
            )
        await callback.answer()

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
