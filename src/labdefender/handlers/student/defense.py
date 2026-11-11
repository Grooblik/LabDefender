import secrets
from datetime import datetime

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.handlers.coursework import require_draft
from labdefender.handlers.defense import (
    show_defense_slots,
    show_queue_assignments,
    show_queue_groups,
    show_student_booking,
    slot_time_label,
)
from labdefender.handlers.notifications import Notifier, notify_queue_booking
from labdefender.keyboards.defense import (
    StudentQueueCallback,
    StudentQueueDraftCallback,
    queue_draft_keyboard,
)
from labdefender.services import queue as queue_service
from labdefender.services.errors import (
    DefenseSlotClosed,
    DefenseSlotFull,
    QueueChanged,
    StaleCourseAction,
)
from labdefender.services.queue import (
    enroll_slot,
    get_defense_slot,
    get_student_booking,
    move_to_end,
    require_approved,
    slot_end_at,
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
            await show_defense_slots(
                callback.message, session, current_user, assignment_id=object_id, offset=offset
            )
        elif action == "status":
            await show_student_booking(
                callback.message, session, current_user, assignment_id=object_id
            )
        else:
            if action == "choose":
                info = await get_defense_slot(session, actor_id=current_user.id, slot_id=object_id)
                slot = info.slot
                assignment_id = slot.assignment_id
                await require_approved(
                    session, actor_id=current_user.id, assignment_id=assignment_id
                )
                booking = await get_student_booking(
                    session, actor_id=current_user.id, assignment_id=assignment_id
                )
                if booking and booking.slot.id == slot.id:
                    await show_student_booking(
                        callback.message, session, current_user, assignment_id=assignment_id
                    )
                    await callback.answer()
                    return
                if (
                    not slot.is_open
                    or slot_end_at(slot.date, slot.end_time) <= queue_service.now_moscow()
                ):
                    raise DefenseSlotClosed()
                if info.occupied >= slot.capacity:
                    raise DefenseSlotFull()
                operation = "enroll"
                label = "Сменить слот" if booking else "Записаться"
                description = f"{label} на {slot.date:%d.%m.%Y} · {slot_time_label(slot)} (Москва)?"
                if booking:
                    description += "\nВ новом слоте вы попадёте в конец очереди."
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
                slot = booking.slot
                if (
                    not slot.is_open
                    or slot_end_at(slot.date, slot.end_time) <= queue_service.now_moscow()
                ):
                    raise DefenseSlotClosed()
                operation = "move"
                description = (
                    "Перенести вашу запись на "
                    f"{slot.date:%d.%m.%Y} · {slot_time_label(slot)} в конец очереди?"
                )
            nonce = secrets.token_hex(8)
            await state.set_data(
                {
                    "operation": operation,
                    "assignment_id": assignment_id,
                    "slot_id": slot.id,
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
        bot: Bot,
        notifier: Notifier,
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
            entry = None
            if data["operation"] == "enroll":
                entry = await enroll_slot(
                    session,
                    actor_id=current_user.id,
                    assignment_id=data["assignment_id"],
                    slot_id=data["slot_id"],
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
            # A repeated enrollment into the current slot changes nothing and is not announced.
            if entry is not None and (
                entry.id != data["entry_id"] or entry.order_at != expected_order
            ):
                await notify_queue_booking(
                    notifier,
                    bot,
                    session,
                    entry_id=entry.id,
                    switched=data["entry_id"] is not None,
                )
            await callback.message.answer("Запись в очереди сохранена.")
            await show_student_booking(
                callback.message, session, current_user, assignment_id=data["assignment_id"]
            )
        await callback.answer()

    return router
