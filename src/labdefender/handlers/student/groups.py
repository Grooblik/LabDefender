from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.keyboards.groups import StudentGroupCallback, group_list_keyboard
from labdefender.services.groups import get_group, list_groups


async def show_groups(
    message: Message, session: AsyncSession, current_user: User, *, offset: int = 0
) -> None:
    page = await list_groups(session, actor_id=current_user.id, offset=offset)
    if not page.groups and offset == 0:
        await message.answer("Вы пока не состоите в группах. Откройте приглашение преподавателя.")
        return
    title = "Ваши группы:" if page.groups else "На этой странице групп нет. Вернитесь назад."
    await message.answer(title, reply_markup=group_list_keyboard(page, role=Role.STUDENT))


def create_router() -> Router:
    router = Router(name="student_groups")

    @router.message(Command("my_groups"))
    async def my_groups(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_groups(message, session, current_user)

    @router.callback_query(StudentGroupCallback.filter())
    async def group_action(
        callback: CallbackQuery,
        callback_data: StudentGroupCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Откройте список заново через /my_groups.")
            return
        await state.clear()
        if callback_data.action == "list":
            await show_groups(callback.message, session, current_user, offset=callback_data.value)
        else:
            group = await get_group(session, actor_id=current_user.id, group_id=callback_data.value)
            await callback.message.answer(
                f"Группа «{group.name}»\nВы состоите в этой группе.\nВаши группы: /my_groups"
            )
        await callback.answer()

    return router
