import secrets
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.handlers.coursework import require_draft
from labdefender.handlers.defense import (
    show_defense_days,
    show_queue_assignments,
    show_queue_groups,
    show_student_booking,
)
from labdefender.keyboards.defense import (
    StudentQueueCallback,
    StudentQueueDraftCallback,
    queue_draft_keyboard,
)
from labdefender.services import queue as queue_service
from labdefender.services.errors import (
    DefenseDayClosed,
    DefenseDayFull,
    QueueChanged,
    StaleCourseAction,
)
from labdefender.services.queue import (
    enroll_day,
    get_defense_day,
    get_student_booking,
    move_to_end,
    require_approved,
)
from labdefender.states.defense import QueueChange


def create_router() -> Router:
    router = Router(name="student_defense")

    @router.message(Command("queue"))
    async def queue(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_queue_groups(message, session, current_user)

    @router.callback_query(StudentQueueCallback.filter())
    async def queue_action(
        callback: CallbackQuery,
        callback_data: StudentQueueCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        await state.clear()
        action, object_id, offset = (
            callback_data.action,
            callback_data.object_id,
            callback_data.offset,
        )
        if action == "groups":
            await show_queue_groups(callback.message, session, current_user, offset=offset)
        elif action == "assignments":
            await show_queue_assignments(
                callback.message, session, current_user, group_id=object_id, offset=offset
            )
        elif action == "days":
            await show_defense_days(
                callback.message, session, current_user, assignment_id=object_id, offset=offset
            )
        elif action == "status":
            await show_student_booking(
                callback.message, session, current_user, assignment_id=object_id
            )
        else:
            if action == "choose":
                info = await get_defense_day(
                    session, actor_id=current_user.id, defense_day_id=object_id
                )
                day = info.day
                assignment_id = day.assignment_id
                await require_approved(
                    session, actor_id=current_user.id, assignment_id=assignment_id
                )
                booking = await get_student_booking(
                    session, actor_id=current_user.id, assignment_id=assignment_id
                )
                if day.date < queue_service.today_moscow():
                    raise DefenseDayClosed()
                if booking and booking.day.id == day.id:
                    await show_student_booking(
                        callback.message, session, current_user, assignment_id=assignment_id
                    )
                    await callback.answer()
                    return
                if info.occupied >= day.capacity:
                    raise DefenseDayFull()
                operation = "enroll"
                label = "Сменить день" if booking else "Записаться"
                description = f"{label} на {day.date:%d.%m.%Y} (Москва)?"
                if booking:
                    description += "\nНа новом дне вы попадёте в конец очереди."
            else:
                assignment_id = object_id
                await require_approved(
                    session, actor_id=current_user.id, assignment_id=assignment_id
                )
                booking = await get_student_booking(
                    session, actor_id=current_user.id, assignment_id=assignment_id
                )
                if booking is None:
                    raise QueueChanged()
                day = booking.day
                if day.date < queue_service.today_moscow():
                    raise DefenseDayClosed()
                operation = "move"
                description = f"Перенести вашу запись на {day.date:%d.%m.%Y} в конец очереди?"
            nonce = secrets.token_hex(8)
            await state.set_data(
                {
                    "operation": operation,
                    "assignment_id": assignment_id,
                    "day_id": day.id,
                    "entry_id": booking.entry.id if booking else None,
                    "order_at": booking.entry.order_at.isoformat() if booking else None,
                    "nonce": nonce,
                }
            )
            await state.set_state(QueueChange.confirmation)
            await callback.message.answer(
                description, reply_markup=queue_draft_keyboard(nonce, teacher=False)
            )
        await callback.answer()

    @router.callback_query(StudentQueueDraftCallback.filter())
    async def confirm_queue(
        callback: CallbackQuery,
        callback_data: StudentQueueDraftCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        data = await require_draft(
            state, expected_state=QueueChange.confirmation.state, nonce=callback_data.nonce
        )
        if callback_data.action == "cancel":
            await state.clear()
            await callback.message.answer(
                "Изменение очереди отменено. Сохранённая запись не изменена."
            )
        else:
            expected_order = datetime.fromisoformat(data["order_at"]) if data["order_at"] else None
            if data["operation"] == "enroll":
                await enroll_day(
                    session,
                    actor_id=current_user.id,
                    assignment_id=data["assignment_id"],
                    defense_day_id=data["day_id"],
                    expected_entry_id=data["entry_id"],
                    expected_order_at=expected_order,
                )
            else:
                await move_to_end(
                    session,
                    actor_id=current_user.id,
                    assignment_id=data["assignment_id"],
                    expected_entry_id=data["entry_id"],
                    expected_order_at=expected_order,
                )
            await state.clear()
            await callback.message.answer("Запись в очереди сохранена.")
            await show_student_booking(
                callback.message, session, current_user, assignment_id=data["assignment_id"]
            )
        await callback.answer()

    return router
