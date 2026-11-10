import re
import secrets
from datetime import date

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
    show_day_queue,
    show_defense_days,
    show_queue_assignments,
    show_queue_groups,
)
from labdefender.keyboards.defense import (
    TeacherDayDraftCallback,
    TeacherQueueCallback,
    queue_draft_keyboard,
)
from labdefender.services.access import require_assignment_access, require_user
from labdefender.services.errors import InvalidDefenseDay, StaleCourseAction
from labdefender.services.queue import create_defense_day, validate_defense_day
from labdefender.states.defense import DefenseDayCreation


def create_router() -> Router:
    router = Router(name="teacher_defense")

    @router.message(Command("new_defense_day"))
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
            await show_defense_days(
                callback.message, session, current_user, assignment_id=object_id, offset=offset
            )
        elif action == "open":
            await show_day_queue(
                callback.message, session, current_user, defense_day_id=object_id, offset=offset
            )
        elif action == "new":
            await require_user(session, actor_id=current_user.id, role=Role.TEACHER)
            assignment = await require_assignment_access(
                session, actor_id=current_user.id, assignment_id=object_id
            )
            await state.set_data({"assignment_id": assignment.id, "title": assignment.title})
            await state.set_state(DefenseDayCreation.date)
            await callback.message.answer(
                f"Задание: {assignment.title}\n"
                "Введите дату защиты YYYY-MM-DD (Москва). Отмена: /cancel."
            )
        await callback.answer()

    @router.message(DefenseDayCreation.date, PlainTextInput())
    async def day_date(message: Message, state: FSMContext) -> None:
        try:
            text = message.text.strip()
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) is None:
                raise ValueError
            value = date.fromisoformat(text)
            validate_defense_day(value, 1)
        except ValueError:
            raise InvalidDefenseDay() from None
        await state.update_data(day_date=value.isoformat())
        await state.set_state(DefenseDayCreation.capacity)
        await message.answer("Введите положительное число мест. Отмена: /cancel.")

    @router.message(DefenseDayCreation.capacity, PlainTextInput())
    async def day_capacity(message: Message, state: FSMContext) -> None:
        try:
            capacity = int(message.text)
        except ValueError:
            raise InvalidDefenseDay() from None
        draft = await state.get_data()
        day_date = date.fromisoformat(draft["day_date"])
        validate_defense_day(day_date, capacity)
        nonce = secrets.token_hex(8)
        await state.update_data(capacity=capacity, nonce=nonce)
        await state.set_state(DefenseDayCreation.confirmation)
        await message.answer(
            f"Открыть день защиты?\nЗадание: {draft['title']}\n"
            f"Дата: {day_date:%d.%m.%Y} (Москва)\nМест: {capacity}",
            reply_markup=queue_draft_keyboard(nonce, teacher=True),
        )

    @router.callback_query(TeacherDayDraftCallback.filter())
    async def confirm_day(
        callback: CallbackQuery,
        callback_data: TeacherDayDraftCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        data = await require_draft(
            state, expected_state=DefenseDayCreation.confirmation.state, nonce=callback_data.nonce
        )
        if callback_data.action == "cancel":
            await state.clear()
            await callback.message.answer("Создание дня защиты отменено.")
        else:
            day = await create_defense_day(
                session,
                actor_id=current_user.id,
                assignment_id=data["assignment_id"],
                day_date=date.fromisoformat(data["day_date"]),
                capacity=data["capacity"],
            )
            await state.clear()
            await callback.message.answer("День защиты сохранён.")
            await show_day_queue(callback.message, session, current_user, defense_day_id=day.id)
        await callback.answer()

    @router.message(StateFilter(DefenseDayCreation.date, DefenseDayCreation.capacity), ~F.text)
    async def text_required(message: Message) -> None:
        await message.answer("Отправьте дату или число мест текстом. Отмена: /cancel.")

    return router
