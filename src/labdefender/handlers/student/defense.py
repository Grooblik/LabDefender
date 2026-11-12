import secrets
from datetime import datetime

from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.handlers.coursework import require_draft
from labdefender.handlers.defense import (
    show_assignment_slots,
    show_group_slots,
    show_queue_groups,
    show_student_booking,
    show_student_defense,
    slot_time_label,
)
from labdefender.handlers.notifications import Notifier, notify_queue_booking
from labdefender.keyboards.coursework import selection_keyboard
from labdefender.keyboards.defense import (
    StudentQueueCallback,
    StudentQueueDraftCallback,
    queue_draft_keyboard,
)
from labdefender.services import queue as queue_service
from labdefender.services.errors import (
    ApprovalRequired,
    DefenseSlotClosed,
    DefenseSlotFull,
    QueueChanged,
    StaleCourseAction,
)
from labdefender.services.queue import (
    enroll_slot,
    get_defense_slot,
    get_student_booking,
    list_defense_works,
    move_to_end,
    require_approved,
    slot_end_at,
)
from labdefender.states.defense import QueueChange


async def _ask_enroll(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    *,
    assignment_id: int,
    slot_id: int,
) -> None:
    """Confirm booking one approved work into a slot of its group (or moving it there)."""
    info = await get_defense_slot(session, actor_id=user.id, slot_id=slot_id)
    slot = info.slot
    submission = await require_approved(session, actor_id=user.id, assignment_id=assignment_id)
    booking = await get_student_booking(session, actor_id=user.id, assignment_id=assignment_id)
    if booking and booking.slot.id == slot.id:
        await show_student_booking(message, session, user, assignment_id=assignment_id)
        return
    if not slot.is_open or slot_end_at(slot.date, slot.end_time) <= queue_service.now_moscow():
        raise DefenseSlotClosed()
    if info.occupied >= slot.capacity:
        raise DefenseSlotFull()
    label = "Сменить слот" if booking else "Записать"
    description = (
        f"{label}: попытка №{submission.attempt_number} на "
        f"{slot.date:%d.%m.%Y} · {slot_time_label(slot)} (Москва)?"
    )
    if booking:
        description += "\nВ новом слоте работа встанет в конец очереди."
    await _ask(message, state, "enroll", assignment_id, slot.id, booking, description)


async def _ask(message, state, operation, assignment_id, slot_id, booking, description) -> None:
    nonce = secrets.token_hex(8)
    await state.set_data(
        {
            "operation": operation,
            "assignment_id": assignment_id,
            "slot_id": slot_id,
            "entry_id": booking.entry.id if booking else None,
            "order_at": booking.entry.order_at.isoformat() if booking else None,
            "nonce": nonce,
        }
    )
    await state.set_state(QueueChange.confirmation)
    await message.answer(description, reply_markup=queue_draft_keyboard(nonce, teacher=False))


async def _choose_work(
    message: Message, session: AsyncSession, user: User, state: FSMContext, *, slot_id: int
) -> None:
    """A slot was chosen (from the list or a notice): which approved work goes there?"""
    info = await get_defense_slot(session, actor_id=user.id, slot_id=slot_id)
    works = await list_defense_works(session, actor_id=user.id, group_id=info.slot.group_id)
    if not works:
        raise ApprovalRequired()
    candidates = [work for work in works if work.booking is None or work.booking.slot.id != slot_id]
    if not candidates:
        await message.answer("Все ваши допущенные работы уже записаны в этот слот.")
        await show_student_defense(message, session, user, group_id=info.slot.group_id)
        return
    if len(candidates) == 1:
        await _ask_enroll(
            message,
            session,
            user,
            state,
            assignment_id=candidates[0].assignment.id,
            slot_id=slot_id,
        )
        return
    buttons = [
        (
            f"{work.assignment.title} · попытка №{work.submission.attempt_number}"
            + (f" · сейчас {work.booking.slot.date:%d.%m}" if work.booking else ""),
            StudentQueueCallback(
                action="pick", object_id=work.assignment.id, slot_id=slot_id
            ).pack(),
        )
        for work in candidates
    ]
    await message.answer(
        f"Какую работу записать на {info.slot.date:%d.%m.%Y} · "
        f"{slot_time_label(info.slot)}? Каждая работа занимает своё место.",
        reply_markup=selection_keyboard(buttons),
    )


def create_router() -> Router:
    router = Router(name="student_defense")

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
        message, action = callback.message, callback_data.action
        object_id, offset = callback_data.object_id, callback_data.offset
        if action == "groups":
            await show_queue_groups(message, session, current_user, offset=offset)
        elif action == "assignments":
            await show_student_defense(message, session, current_user, group_id=object_id)
        elif action == "slots":
            await show_group_slots(
                message, session, current_user, group_id=object_id, offset=offset
            )
        elif action == "days":
            await show_assignment_slots(
                message, session, current_user, assignment_id=object_id, offset=offset
            )
        elif action == "status":
            await show_student_booking(message, session, current_user, assignment_id=object_id)
        elif action == "choose":
            await _choose_work(message, session, current_user, state, slot_id=object_id)
        elif action == "pick":
            await _ask_enroll(
                message,
                session,
                current_user,
                state,
                assignment_id=object_id,
                slot_id=callback_data.slot_id,
            )
        else:
            assignment_id = object_id
            await require_approved(session, actor_id=current_user.id, assignment_id=assignment_id)
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
            await _ask(
                message,
                state,
                "move",
                assignment_id,
                slot.id,
                booking,
                "Перенести вашу запись на "
                f"{slot.date:%d.%m.%Y} · {slot_time_label(slot)} в конец очереди?",
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
