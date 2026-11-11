"""Teacher's slot creation wizard: buttons for date, start, duration and capacity.

Every step also accepts the value as text, so presets are optional. The FSM keeps only the
draft and a fresh nonce per step: buttons of a previous step or draft become stale.
Final validation and the write remain in `services.queue.create_defense_slot`.
"""

import re
import secrets
from datetime import date, time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.filters.plain_text import PlainTextInput
from labdefender.handlers.coursework import require_draft
from labdefender.handlers.defense import show_slot_queue
from labdefender.handlers.notifications import Notifier, notify_slot_opened
from labdefender.keyboards.defense import TeacherDayDraftCallback
from labdefender.keyboards.slots import (
    MINUTES_PER_DAY,
    TeacherSlotCallback,
    calendar_keyboard,
    calendar_window,
    capacity_keyboard,
    duration_keyboard,
    duration_options,
    from_minutes,
    interval_label,
    parse_date_value,
    range_label,
    slot_confirmation_keyboard,
    start_keyboard,
    start_options,
    template_fits,
    to_minutes,
    weekday_label,
)
from labdefender.services import queue as queue_service
from labdefender.services.access import (
    require_assignment_access,
    require_defense_slot_access,
    require_user,
)
from labdefender.services.errors import InvalidDefenseSlot, StaleCourseAction
from labdefender.services.queue import (
    create_defense_slot,
    latest_slot_template,
    list_slots_between,
    validate_defense_slot,
)
from labdefender.states.defense import DefenseSlotCreation

WIZARD_STATES = {
    DefenseSlotCreation.date.state,
    DefenseSlotCreation.start_time.state,
    DefenseSlotCreation.end_time.state,
    DefenseSlotCreation.capacity.state,
    DefenseSlotCreation.confirmation.state,
}
# Draft keys that stay valid when the wizard (re)enters a step.
STEP_KEYS = {
    DefenseSlotCreation.date: ("assignment_id", "title", "page"),
    DefenseSlotCreation.start_time: ("assignment_id", "title", "page", "day_date"),
    DefenseSlotCreation.end_time: ("assignment_id", "title", "page", "day_date", "start_time"),
    DefenseSlotCreation.capacity: (
        "assignment_id",
        "title",
        "page",
        "day_date",
        "start_time",
        "end_time",
    ),
    DefenseSlotCreation.confirmation: (
        "assignment_id",
        "title",
        "page",
        "day_date",
        "start_time",
        "end_time",
        "capacity",
    ),
}


def parse_time(text: str) -> time:
    value = text.strip()
    if re.fullmatch(r"[0-9]{2}:[0-9]{2}", value) is None:
        raise ValueError
    return time.fromisoformat(value)


def _busy(slots) -> list[tuple[time, time]]:
    return [(slot.start_time, slot.end_time) for slot in slots]


def _busy_line(slots) -> str:
    if not slots:
        return "Других слотов задания на этот день нет."
    return "Уже есть: " + ", ".join(interval_label(s.start_time, s.end_time) for s in slots) + "."


async def _enter(state: FSMContext, step, **values) -> str:
    """Switch to a step, drop values of later steps and issue a fresh nonce."""
    data = await state.get_data()
    draft = {key: data[key] for key in STEP_KEYS[step] if key in data}
    draft.update(values)
    draft["nonce"] = secrets.token_hex(8)
    await state.set_data(draft)
    await state.set_state(step)
    return draft["nonce"]


async def _show(message: Message, text: str, markup: InlineKeyboardMarkup | None, *, edit: bool):
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass  # Too old, deleted or unchanged message: send a new one instead.
    await message.answer(text, reply_markup=markup)


async def _show_calendar(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    *,
    page: int,
    edit: bool,
) -> None:
    data = await state.get_data()
    today = queue_service.today_moscow()
    try:
        first, last = calendar_window(today, page)
    except ValueError:
        raise StaleCourseAction() from None
    slots = await list_slots_between(
        session,
        actor_id=user.id,
        assignment_id=data["assignment_id"],
        first_day=first,
        last_day=last,
    )
    nonce = await _enter(state, DefenseSlotCreation.date, page=page)
    await _show(
        message,
        f"Задание: {data['title']}\n"
        f"Выберите дату слота (Москва), {range_label(first, last)}.\n"
        "• — на дату уже есть слоты задания.\n"
        "Или отправьте дату текстом YYYY-MM-DD. Отмена: /cancel.",
        calendar_keyboard(
            today=today,
            page=page,
            marked={slot.date for slot in slots},
            nonce=nonce,
        ),
        edit=edit,
    )


async def _day_slots(session: AsyncSession, user: User, data: dict) -> tuple[date, tuple]:
    day = date.fromisoformat(data["day_date"])
    slots = await list_slots_between(
        session,
        actor_id=user.id,
        assignment_id=data["assignment_id"],
        first_day=day,
        last_day=day,
    )
    return day, slots


async def _show_start(
    message: Message, session: AsyncSession, user: User, state: FSMContext, *, edit: bool
) -> None:
    data = await state.get_data()
    day, slots = await _day_slots(session, user, data)
    now = queue_service.now_moscow()
    busy = _busy(slots)
    options = start_options(day, busy, now)
    template = await latest_slot_template(
        session, actor_id=user.id, assignment_id=data["assignment_id"]
    )
    repeat = None
    if template is not None and template_fits(
        day, template.start_time, template.end_time, busy, now
    ):
        repeat = (
            template.id,
            "Как в прошлый раз: "
            f"{interval_label(template.start_time, template.end_time)} · мест: {template.capacity}",
        )
    nonce = await _enter(state, DefenseSlotCreation.start_time)
    hint = "" if options else "\nСвободного времени в сетке нет — отправьте время текстом."
    await _show(
        message,
        f"Задание: {data['title']}\n"
        f"Дата: {day:%d.%m.%Y} ({weekday_label(day)})\n"
        f"{_busy_line(slots)}\n"
        f"Выберите время начала (Москва) или отправьте HH:MM. Отмена: /cancel.{hint}",
        start_keyboard(options, template=repeat, nonce=nonce),
        edit=edit,
    )


async def _show_end(
    message: Message, session: AsyncSession, user: User, state: FSMContext, *, edit: bool
) -> None:
    data = await state.get_data()
    day, slots = await _day_slots(session, user, data)
    start = time.fromisoformat(data["start_time"])
    options = duration_options(day, to_minutes(start), _busy(slots), queue_service.now_moscow())
    nonce = await _enter(state, DefenseSlotCreation.end_time)
    await _show(
        message,
        f"Задание: {data['title']}\n"
        f"Дата: {day:%d.%m.%Y} ({weekday_label(day)}), начало {start:%H:%M}.\n"
        f"{_busy_line(slots)}\n"
        "Выберите длительность или отправьте время окончания HH:MM. Отмена: /cancel.",
        duration_keyboard(options, nonce=nonce),
        edit=edit,
    )


async def _show_capacity(message: Message, state: FSMContext, *, edit: bool) -> None:
    data = await state.get_data()
    day = date.fromisoformat(data["day_date"])
    start, end = time.fromisoformat(data["start_time"]), time.fromisoformat(data["end_time"])
    nonce = await _enter(state, DefenseSlotCreation.capacity)
    await _show(
        message,
        f"Задание: {data['title']}\n"
        f"Дата: {day:%d.%m.%Y} ({weekday_label(day)}), {interval_label(start, end)}.\n"
        "Выберите число мест или отправьте положительное число. Отмена: /cancel.",
        capacity_keyboard(nonce=nonce),
        edit=edit,
    )


async def _show_confirmation(message: Message, state: FSMContext, *, edit: bool) -> None:
    data = await state.get_data()
    day = date.fromisoformat(data["day_date"])
    start, end = time.fromisoformat(data["start_time"]), time.fromisoformat(data["end_time"])
    validate_defense_slot(day, start, end, data["capacity"])
    nonce = await _enter(state, DefenseSlotCreation.confirmation)
    await _show(
        message,
        f"Открыть слот защиты?\nЗадание: {data['title']}\n"
        f"Дата: {day:%d.%m.%Y} (Москва)\n"
        f"Время: {interval_label(start, end)}\nМест: {data['capacity']}",
        slot_confirmation_keyboard(nonce=nonce),
        edit=edit,
    )


async def start_slot_wizard(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    *,
    assignment_id: int,
) -> None:
    await require_user(session, actor_id=user.id, role=Role.TEACHER)
    assignment = await require_assignment_access(
        session, actor_id=user.id, assignment_id=assignment_id
    )
    await state.set_data({"assignment_id": assignment.id, "title": assignment.title})
    await _show_calendar(message, session, user, state, page=0, edit=False)


def create_router() -> Router:
    router = Router(name="teacher_slots")

    @router.callback_query(TeacherSlotCallback.filter(F.action == "noop"))
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(TeacherSlotCallback.filter())
    async def wizard_button(
        callback: CallbackQuery,
        callback_data: TeacherSlotCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        current = await state.get_state()
        data = await state.get_data()
        if current not in WIZARD_STATES or data.get("nonce") != callback_data.nonce:
            raise StaleCourseAction()
        action, value, message = callback_data.action, callback_data.value, callback.message
        step = {
            DefenseSlotCreation.date.state: "date",
            DefenseSlotCreation.start_time.state: "start",
            DefenseSlotCreation.end_time.state: "duration",
            DefenseSlotCreation.capacity.state: "capacity",
            DefenseSlotCreation.confirmation.state: "confirmation",
        }[current]
        if action == "cancel":
            await state.clear()
            await _show(message, "Создание слота защиты отменено.", None, edit=True)
        elif action == "page" and step == "date":
            await _show_calendar(message, session, current_user, state, page=value, edit=True)
        elif action == "date" and step == "date":
            try:
                day = parse_date_value(value)
            except ValueError:
                raise InvalidDefenseSlot() from None
            if day < queue_service.today_moscow():
                raise InvalidDefenseSlot()
            await state.update_data(day_date=day.isoformat())
            await _show_start(message, session, current_user, state, edit=True)
        elif action == "start" and step == "start":
            try:
                start = from_minutes(value)
            except ValueError:
                raise InvalidDefenseSlot() from None
            await state.update_data(start_time=start.isoformat())
            await _show_end(message, session, current_user, state, edit=True)
        elif action == "duration" and step == "duration":
            start = time.fromisoformat(data["start_time"])
            end_minutes = to_minutes(start) + value
            if value <= 0 or end_minutes >= MINUTES_PER_DAY:
                raise InvalidDefenseSlot()
            end = from_minutes(end_minutes)
            validate_defense_slot(date.fromisoformat(data["day_date"]), start, end, 1)
            await state.update_data(end_time=end.isoformat())
            await _show_capacity(message, state, edit=True)
        elif action == "capacity" and step == "capacity":
            validate_defense_slot(
                date.fromisoformat(data["day_date"]),
                time.fromisoformat(data["start_time"]),
                time.fromisoformat(data["end_time"]),
                value,
            )
            await state.update_data(capacity=value)
            await _show_confirmation(message, state, edit=True)
        elif action == "repeat" and step == "start":
            # The ID is only a pointer: access to the template is checked again.
            template = await require_defense_slot_access(
                session, actor_id=current_user.id, slot_id=value
            )
            validate_defense_slot(
                date.fromisoformat(data["day_date"]),
                template.start_time,
                template.end_time,
                template.capacity,
            )
            await state.update_data(
                start_time=template.start_time.isoformat(),
                end_time=template.end_time.isoformat(),
                capacity=template.capacity,
            )
            await _show_confirmation(message, state, edit=True)
        elif action == "back" and step == "start":
            await _show_calendar(
                message, session, current_user, state, page=data.get("page", 0), edit=True
            )
        elif action == "back" and step == "duration":
            await _show_start(message, session, current_user, state, edit=True)
        elif action == "back" and step == "capacity":
            await _show_end(message, session, current_user, state, edit=True)
        elif action == "back" and step == "confirmation":
            await _show_capacity(message, state, edit=True)
        else:
            raise StaleCourseAction()
        await callback.answer()

    @router.message(DefenseSlotCreation.date, PlainTextInput())
    async def slot_date(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        try:
            text = message.text.strip()
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) is None:
                raise ValueError
            value = date.fromisoformat(text)
            if value < queue_service.today_moscow():
                raise ValueError
        except ValueError:
            raise InvalidDefenseSlot() from None
        await state.update_data(day_date=value.isoformat())
        await _show_start(message, session, current_user, state, edit=False)

    @router.message(DefenseSlotCreation.start_time, PlainTextInput())
    async def slot_start_time(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        try:
            value = parse_time(message.text)
        except ValueError:
            raise InvalidDefenseSlot() from None
        await state.update_data(start_time=value.isoformat())
        await _show_end(message, session, current_user, state, edit=False)

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
        await _show_capacity(message, state, edit=False)

    @router.message(DefenseSlotCreation.capacity, PlainTextInput())
    async def slot_capacity(message: Message, state: FSMContext) -> None:
        try:
            capacity = int(message.text)
        except ValueError:
            raise InvalidDefenseSlot() from None
        draft = await state.get_data()
        validate_defense_slot(
            date.fromisoformat(draft["day_date"]),
            time.fromisoformat(draft["start_time"]),
            time.fromisoformat(draft["end_time"]),
            capacity,
        )
        await state.update_data(capacity=capacity)
        await _show_confirmation(message, state, edit=False)

    @router.callback_query(TeacherDayDraftCallback.filter())
    async def confirm_slot(
        callback: CallbackQuery,
        callback_data: TeacherDayDraftCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        bot: Bot,
        notifier: Notifier,
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
            await notify_slot_opened(notifier, bot, session, slot_id=slot.id)
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
        await message.answer(
            "Выберите вариант кнопкой или отправьте дату, время или число мест текстом. "
            "Отмена: /cancel."
        )

    return router
