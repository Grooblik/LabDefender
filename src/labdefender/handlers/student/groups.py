from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.handlers.coursework import answer_no_groups
from labdefender.keyboards.groups import (
    StudentGroupCallback,
    group_list_keyboard,
    student_assignments_keyboard,
)
from labdefender.services.groups import get_group, list_groups


async def show_groups(
    message: Message, session: AsyncSession, current_user: User, *, offset: int = 0
) -> None:
    page = await list_groups(session, actor_id=current_user.id, offset=offset)
    if not page.groups and offset == 0:
        await answer_no_groups(message, current_user)
        return
    title = "Ваши группы:" if page.groups else "На этой странице групп нет. Вернитесь назад."
    await message.answer(title, reply_markup=group_list_keyboard(page, role=Role.STUDENT))


async def show_student_group(
    message: Message, session: AsyncSession, current_user: User, *, group_id: int
) -> None:
    group = await get_group(session, actor_id=current_user.id, group_id=group_id)
    await message.answer(
        f"Группа «{group.name}»\nВы состоите в этой группе.",
        reply_markup=student_assignments_keyboard(group.id),
    )


def create_router() -> Router:
    router = Router(name="student_groups")

    @router.callback_query(StudentGroupCallback.filter())
    async def group_action(
        callback: CallbackQuery,
        callback_data: StudentGroupCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Откройте список заново: «👥 Мои группы».")
            return
        await state.clear()
        if callback_data.action == "list":
            await show_groups(callback.message, session, current_user, offset=callback_data.value)
        else:
            await show_student_group(
                callback.message, session, current_user, group_id=callback_data.value
            )
        await callback.answer()

    return router
