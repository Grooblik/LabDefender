from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.handlers.defense import (
    show_defense_slots,
    show_queue_assignments,
    show_queue_groups,
    show_slot_queue,
)
from labdefender.handlers.notifications import Notifier, notify_slot_opened
from labdefender.handlers.teacher.slots import start_slot_wizard
from labdefender.keyboards.defense import TeacherQueueCallback
from labdefender.services.errors import StaleCourseAction
from labdefender.services.queue import (
    cancel_defense_slot,
    close_defense_slot,
    reopen_defense_slot,
)


def create_router() -> Router:
    router = Router(name="teacher_defense")

    @router.callback_query(TeacherQueueCallback.filter())
    async def queue_action(
        callback: CallbackQuery,
        callback_data: TeacherQueueCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        bot: Bot,
        notifier: Notifier,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        await state.clear()
        action, object_id, offset = (
            callback_data.action,
            callback_data.object_id,
            callback_data.offset,
        )
        if action in {"groups_new", "groups_view"}:
            await show_queue_groups(
                callback.message, session, current_user, new=action == "groups_new", offset=offset
            )
        elif action in {"assignments_new", "assignments_view"}:
            await show_queue_assignments(
                callback.message,
                session,
                current_user,
                group_id=object_id,
                new=action == "assignments_new",
                offset=offset,
            )
        elif action == "days":
            await show_defense_slots(
                callback.message, session, current_user, assignment_id=object_id, offset=offset
            )
        elif action == "open":
            await show_slot_queue(
                callback.message, session, current_user, slot_id=object_id, offset=offset
            )
        elif action == "close":
            await close_defense_slot(session, actor_id=current_user.id, slot_id=object_id)
            await callback.message.answer("Слот закрыт для новых записей.")
            await show_slot_queue(
                callback.message, session, current_user, slot_id=object_id, offset=offset
            )
        elif action == "reopen":
            result = await reopen_defense_slot(session, actor_id=current_user.id, slot_id=object_id)
            if result.reopened:
                await notify_slot_opened(notifier, bot, session, slot_id=object_id, reopened=True)
            await callback.message.answer("Слот снова открыт для записи.")
            await show_slot_queue(
                callback.message, session, current_user, slot_id=object_id, offset=offset
            )
        elif action == "cancel":
            slot = await cancel_defense_slot(session, actor_id=current_user.id, slot_id=object_id)
            await callback.message.answer("Пустой слот отменён.")
            await show_defense_slots(
                callback.message,
                session,
                current_user,
                assignment_id=slot.assignment_id,
            )
        elif action == "new":
            await start_slot_wizard(
                callback.message, session, current_user, state, assignment_id=object_id
            )
        await callback.answer()

    return router
