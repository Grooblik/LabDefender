import re
import secrets
from datetime import date, time

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.filters.plain_text import PlainTextInput
from labdefender.handlers.coursework import require_draft
from labdefender.handlers.defense import (
    show_defense_slots,
    show_queue_assignments,
    show_queue_groups,
    show_slot_queue,
)
from labdefender.keyboards.defense import (
    TeacherDayDraftCallback,
    TeacherQueueCallback,
    queue_draft_keyboard,
)
from labdefender.services.access import require_assignment_access, require_user
from labdefender.services.errors import InvalidDefenseSlot, StaleCourseAction
from labdefender.services.queue import (
    cancel_defense_slot,
    close_defense_slot,
    create_defense_slot,
    reopen_defense_slot,
    today_moscow,
    validate_defense_slot,
)
from labdefender.states.defense import DefenseSlotCreation


def parse_time(text: str) -> time:
    value = text.strip()
    if re.fullmatch(r"[0-9]{2}:[0-9]{2}", value) is None:
        raise ValueError
    return time.fromisoformat(value)


def create_router() -> Router:
    router = Router(name="teacher_defense")

    @router.message(Command("new_defense_day", "new_slot"))
    async def new_day(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_queue_groups(message, session, current_user, new=True)

    @router.message(Command("queue"))
    async def queue(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_queue_groups(message, session, current_user)

    @router.callback_query(TeacherQueueCallback.filter())
    async def queue_action(
        callback: CallbackQuery,
        callback_data: TeacherQueueCallback,
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
            await reopen_defense_slot(session, actor_id=current_user.id, slot_id=object_id)
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
            await require_user(session, actor_id=current_user.id, role=Role.TEACHER)
            assignment = await require_assignment_access(
                session, actor_id=current_user.id, assignment_id=object_id
            )
            await state.set_data({"assignment_id": assignment.id, "title": assignment.title})
            await state.set_state(DefenseSlotCreation.date)
            await callback.message.answer(
                f"Задание: {assignment.title}\n"
                "Введите дату слота YYYY-MM-DD (Москва). Отмена: /cancel."
            )
        await callback.answer()

    @router.message(DefenseSlotCreation.date, PlainTextInput())
    async def slot_date(message: Message, state: FSMContext) -> None:
        try:
            text = message.text.strip()
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) is None:
                raise ValueError
            value = date.fromisoformat(text)
            if value < today_moscow():
                raise ValueError
        except ValueError:
            raise InvalidDefenseSlot() from None
        await state.update_data(day_date=value.isoformat())
        await state.set_state(DefenseSlotCreation.start_time)
        await message.answer("Введите время начала HH:MM (Москва). Отмена: /cancel.")

    @router.message(DefenseSlotCreation.start_time, PlainTextInput())
    async def slot_start_time(message: Message, state: FSMContext) -> None:
        try:
            value = parse_time(message.text)
        except ValueError:
            raise InvalidDefenseSlot() from None
        await state.update_data(start_time=value.isoformat())
        await state.set_state(DefenseSlotCreation.end_time)
        await message.answer("Введите время окончания HH:MM (Москва). Отмена: /cancel.")

    @router.message(DefenseSlotCreation.end_time, PlainTextInput())
    async def slot_end_time(message: Message, state: FSMContext) -> None:
        try:
            value = parse_time(message.text)
            draft = await state.get_data()
            validate_defense_slot(
                date.fromisoformat(draft["day_date"]),
                time.fromisoformat(draft["start_time"]),
                value,
                1,
            )
        except ValueError:
            raise InvalidDefenseSlot() from None
        await state.update_data(end_time=value.isoformat())
        await state.set_state(DefenseSlotCreation.capacity)
        await message.answer("Введите положительное число мест. Отмена: /cancel.")

    @router.message(DefenseSlotCreation.capacity, PlainTextInput())
    async def slot_capacity(message: Message, state: FSMContext) -> None:
        try:
            capacity = int(message.text)
        except ValueError:
            raise InvalidDefenseSlot() from None
        draft = await state.get_data()
        day_date = date.fromisoformat(draft["day_date"])
        start_time = time.fromisoformat(draft["start_time"])
        end_time = time.fromisoformat(draft["end_time"])
        validate_defense_slot(day_date, start_time, end_time, capacity)
        nonce = secrets.token_hex(8)
        await state.update_data(capacity=capacity, nonce=nonce)
        await state.set_state(DefenseSlotCreation.confirmation)
        await message.answer(
            f"Открыть слот защиты?\nЗадание: {draft['title']}\n"
            f"Дата: {day_date:%d.%m.%Y} (Москва)\n"
            f"Время: {start_time:%H:%M}–{end_time:%H:%M}\nМест: {capacity}",
            reply_markup=queue_draft_keyboard(nonce, teacher=True),
        )

    @router.callback_query(TeacherDayDraftCallback.filter())
    async def confirm_slot(
        callback: CallbackQuery,
        callback_data: TeacherDayDraftCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        data = await require_draft(
            state, expected_state=DefenseSlotCreation.confirmation.state, nonce=callback_data.nonce
        )
        if callback_data.action == "cancel":
            await state.clear()
            await callback.message.answer("Создание слота защиты отменено.")
        else:
            slot = await create_defense_slot(
                session,
                actor_id=current_user.id,
                assignment_id=data["assignment_id"],
                day_date=date.fromisoformat(data["day_date"]),
                start_time=time.fromisoformat(data["start_time"]),
                end_time=time.fromisoformat(data["end_time"]),
                capacity=data["capacity"],
            )
            await state.clear()
            await callback.message.answer("Слот защиты сохранён.")
            await show_slot_queue(callback.message, session, current_user, slot_id=slot.id)
        await callback.answer()

    @router.message(
        StateFilter(
            DefenseSlotCreation.date,
            DefenseSlotCreation.start_time,
            DefenseSlotCreation.end_time,
            DefenseSlotCreation.capacity,
        ),
        ~F.text,
    )
    async def text_required(message: Message) -> None:
        await message.answer("Отправьте дату, время или число мест текстом. Отмена: /cancel.")

    return router
