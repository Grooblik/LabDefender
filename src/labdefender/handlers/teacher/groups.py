import secrets

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.handlers.coursework import answer_no_groups
from labdefender.keyboards.groups import (
    GroupCreationCallback,
    TeacherGroupCallback,
    group_confirmation_keyboard,
    group_list_keyboard,
    teacher_group_keyboard,
)
from labdefender.keyboards.menu import cancel_keyboard
from labdefender.services.errors import StaleGroupAction
from labdefender.services.groups import (
    create_group,
    get_group,
    get_invite_link,
    list_groups,
    normalize_group_name,
)
from labdefender.states.groups import GroupCreation


async def show_groups(
    message: Message,
    session: AsyncSession,
    current_user: User,
    *,
    offset: int = 0,
    invite: bool = False,
) -> None:
    page = await list_groups(session, actor_id=current_user.id, offset=offset)
    if not page.groups and offset == 0:
        await answer_no_groups(message, current_user)
        return
    title = "Выберите группу для приглашения:" if invite else "Ваши группы:"
    if not page.groups:
        title = "На этой странице групп нет. Вернитесь назад."
    await message.answer(
        title, reply_markup=group_list_keyboard(page, role=Role.TEACHER, invite=invite)
    )


async def show_invite(
    message: Message, session: AsyncSession, current_user: User, bot: Bot, *, group_id: int
) -> None:
    group = await get_group(session, actor_id=current_user.id, group_id=group_id)
    username = (await bot.me()).username or ""
    link = await get_invite_link(
        session, actor_id=current_user.id, group_id=group_id, bot_username=username
    )
    await message.answer(
        f"Приглашение в группу «{group.name}»:\n{link}",
        reply_markup=teacher_group_keyboard(group.id),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def start_group_creation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(GroupCreation.waiting_for_name)
    await message.answer(
        "Введите название группы (до 128 символов).", reply_markup=cancel_keyboard()
    )


def create_router() -> Router:
    router = Router(name="teacher_groups")

    @router.callback_query(TeacherGroupCallback.filter())
    async def group_action(
        callback: CallbackQuery,
        callback_data: TeacherGroupCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Откройте список заново: «👥 Мои группы».")
            return
        await state.clear()
        action, value = callback_data.action, callback_data.value
        if action in {"list", "invite_list"}:
            await show_groups(
                callback.message,
                session,
                current_user,
                offset=value,
                invite=action == "invite_list",
            )
        elif action == "invite":
            await show_invite(callback.message, session, current_user, bot, group_id=value)
        elif action == "create":
            await start_group_creation(callback.message, state)
        else:
            group = await get_group(session, actor_id=current_user.id, group_id=value)
            await callback.message.answer(
                f"Группа «{group.name}»", reply_markup=teacher_group_keyboard(group.id)
            )
        await callback.answer()

    @router.message(GroupCreation.waiting_for_name, F.text, ~F.text.startswith("/"))
    async def group_name(message: Message, state: FSMContext) -> None:
        name = normalize_group_name(message.text)
        nonce = secrets.token_hex(8)
        await state.set_data({"group_name": name, "confirmation_id": nonce})
        await state.set_state(GroupCreation.waiting_for_confirmation)
        await message.answer(
            f"Создать группу «{name}»?", reply_markup=group_confirmation_keyboard(nonce)
        )

    @router.message(GroupCreation.waiting_for_name, ~F.text)
    async def name_requires_text(message: Message) -> None:
        await message.answer(
            "Отправьте название группы текстом или нажмите «Отмена».",
            reply_markup=cancel_keyboard(),
        )

    @router.callback_query(GroupCreationCallback.filter())
    async def confirm_creation(
        callback: CallbackQuery,
        callback_data: GroupCreationCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        draft = await state.get_data()
        if (
            await state.get_state() != GroupCreation.waiting_for_confirmation.state
            or draft.get("confirmation_id") != callback_data.nonce
            or not isinstance(draft.get("group_name"), str)
            or not isinstance(callback.message, Message)
        ):
            raise StaleGroupAction()
        if callback_data.action == "cancel":
            await state.clear()
            await callback.message.answer("Создание группы отменено.")
        else:
            group = await create_group(session, actor_id=current_user.id, name=draft["group_name"])
            # Clear before Telegram calls so delivery failures cannot create a duplicate on retry.
            await state.clear()
            await callback.message.answer(f"Группа «{group.name}» создана.")
            await show_invite(callback.message, session, current_user, bot, group_id=group.id)
        await callback.answer()

    return router
