from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import test_queue
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import func, select
from test_queue import TODAY, day, first_button, message_update, press

from labdefender.app import create_dispatcher
from labdefender.db.models import DefenseSlot
from labdefender.enums import AssignmentFormat
from labdefender.keyboards.slots import (
    TeacherSlotCallback,
    calendar_keyboard,
    capacity_keyboard,
    duration_keyboard,
    slot_confirmation_keyboard,
    start_keyboard,
)
from labdefender.services.assignments import create_assignment
from labdefender.services.groups import create_group
from labdefender.services.queue import create_defense_slot
from labdefender.services.slot_grid import (
    WEEKDAYS,
    calendar_pages,
    calendar_window,
    duration_label,
    duration_options,
    range_label,
    start_options,
)

NONCE = "0123456789abcdef"
MOSCOW = ZoneInfo("Europe/Moscow")
# Reuse the queue fixture: teacher 101 with group, assignment and three approved students.
context = test_queue.context


def texts(row):
    return [button.text for button in row]


def screens(bot):
    return [call for call in bot.session.calls if isinstance(call, SendMessage | EditMessageText)]


def screen(bot):
    return screens(bot)[-1]


def buttons(bot):
    return {
        button.text: button.callback_data
        for row in screen(bot).reply_markup.inline_keyboard
        for button in row
    }


async def open_wizard(dispatcher, bot):
    # «Новый слот» → group: slots belong to a group, there is no assignment step.
    await dispatcher.feed_update(bot, message_update("/new_slot"))
    await press(dispatcher, bot, first_button(bot))


async def slots(sessions):
    async with sessions() as session:
        return (
            await session.scalars(select(DefenseSlot).order_by(DefenseSlot.date, DefenseSlot.id))
        ).all()


def test_calendar_mirrors_leading_dots_at_the_end():
    markup = calendar_keyboard(
        today=date(2026, 11, 25), page=0, marked={date(2026, 11, 26)}, nonce=NONCE
    )
    rows = markup.inline_keyboard
    assert texts(rows[0]) == list(WEEKDAYS)
    assert texts(rows[1]) == ["·", "·", "25", "26•", "27", "28", "29"]
    assert texts(rows[2]) == ["30", "1.12", "2", "3", "4", "5", "6"]
    assert texts(rows[3]) == ["7", "8", "9", "10", "11", "·", "·"]
    assert texts(rows[4]) == ["Позже →"]
    assert texts(rows[5]) == ["Отмена"]
    assert TeacherSlotCallback.unpack(rows[1][2].callback_data).value == 20261125
    assert TeacherSlotCallback.unpack(rows[1][0].callback_data).action == "noop"

    later = calendar_keyboard(today=date(2026, 11, 25), page=1, marked=(), nonce=NONCE)
    rows = later.inline_keyboard
    assert texts(rows[1]) == ["·", "·", "·", "·", "·", "12", "13"]
    assert texts(rows[3]) == ["21", "22", "23", "24", "25", "·", "·"]
    assert texts(rows[4]) == ["← Раньше", "Позже →"]
    pages = calendar_pages(date(2026, 11, 25))
    last = calendar_keyboard(today=date(2026, 11, 25), page=len(pages) - 1, marked=(), nonce=NONCE)
    assert texts(last.inline_keyboard[-2]) == ["← Раньше"]
    with pytest.raises(ValueError):
        calendar_window(date(2026, 11, 25), len(pages))


@pytest.mark.parametrize("weekday", range(7))
def test_calendar_pages_are_contiguous_three_rows_and_at_least_two_weeks(weekday):
    today = date(2026, 11, 23) + timedelta(days=weekday)
    pages = calendar_pages(today)
    assert pages[0][0] == today
    assert pages[-1][0] <= today + timedelta(days=365) < pages[-1][1] + timedelta(days=1)
    for (_, last), (following, _) in zip(pages, pages[1:], strict=False):
        assert following == last + timedelta(days=1)
    for page, (first, last) in enumerate(pages):
        days = (last - first).days + 1
        rows = calendar_keyboard(today=today, page=page, marked=(), nonce=NONCE).inline_keyboard
        weeks = rows[1:-2]
        leading = texts(weeks[0]).count("·")
        trailing = texts(weeks[-1]).count("·")
        assert len(weeks) == 3 and days >= 14
        assert leading == first.weekday() and trailing == min(leading, 7 - leading)


def test_every_wizard_callback_fits_telegram_limit():
    markups = [
        calendar_keyboard(
            today=date(2026, 12, 31),
            page=len(calendar_pages(date(2026, 12, 31))) - 1,
            marked=(),
            nonce=NONCE,
        ),
        start_keyboard([8 * 60, 20 * 60 + 30], template=(2**63 - 1, "Как раньше"), nonce=NONCE),
        duration_keyboard([(240, 23 * 60)], nonce=NONCE),
        capacity_keyboard(nonce=NONCE),
        slot_confirmation_keyboard(nonce=NONCE),
    ]
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64


def test_presets_skip_past_overlapping_and_after_midnight_times():
    today = date(2026, 11, 10)
    now = datetime(2026, 11, 10, 10, 0, 30, tzinfo=MOSCOW)
    starts = start_options(today, [], now)
    assert starts[0] == 10 * 60 + 30 and starts[-1] == 20 * 60 + 30
    busy = [(time(14, 0), time(16, 0))]
    tomorrow = today + timedelta(days=1)
    starts = start_options(tomorrow, busy, now)
    assert 13 * 60 + 30 in starts and 16 * 60 in starts
    assert not {14 * 60, 14 * 60 + 30, 15 * 60, 15 * 60 + 30} & set(starts)
    assert duration_options(tomorrow, 13 * 60, busy, now) == [(30, 13 * 60 + 30), (60, 14 * 60)]
    assert duration_options(tomorrow, 22 * 60 + 30, [], now) == [(30, 23 * 60), (60, 23 * 60 + 30)]
    assert duration_options(today, 9 * 60, [], now) == [
        (90, 10 * 60 + 30),
        (120, 11 * 60),
        (180, 12 * 60),
        (240, 13 * 60),
    ]


def test_labels():
    assert [duration_label(value) for value in (30, 60, 90, 75)] == [
        "30 мин",
        "1 ч",
        "1,5 ч",
        "1 ч 15 мин",
    ]
    assert range_label(date(2026, 11, 10), date(2026, 11, 23)) == "10–23 ноября 2026"
    assert range_label(date(2026, 11, 24), date(2026, 12, 7)) == "24 ноября – 7 декабря 2026"
    assert range_label(date(2026, 12, 25), date(2027, 1, 7)) == "25 декабря 2026 – 7 января 2027"


async def test_buttons_create_slot_and_old_buttons_become_stale(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await open_wizard(dispatcher, bot)
    assert isinstance(screen(bot), SendMessage)
    assert "10–28 ноября 2026" in screen(bot).text
    calendar = buttons(bot)
    await press(dispatcher, bot, calendar["11"])
    assert isinstance(screen(bot), EditMessageText)
    assert "Дата: 11.11.2026 (ср)" in screen(bot).text
    assert "Других слотов задания на этот день нет." in screen(bot).text
    await press(dispatcher, bot, buttons(bot)["14:00"])
    await press(dispatcher, bot, buttons(bot)["2 ч · до 16:00"])
    await press(dispatcher, bot, buttons(bot)["10"])
    assert "Время: 14:00–16:00\nМест: 10" in screen(bot).text
    await press(dispatcher, bot, calendar["12"])
    assert "устарела" in bot.session.calls[-1].text
    confirmation = buttons(bot)["Подтвердить"]
    await press(dispatcher, bot, confirmation)
    await press(dispatcher, bot, confirmation)
    assert "устарела" in bot.session.calls[-1].text
    [slot] = await slots(sessions)
    assert (slot.date, slot.start_time, slot.end_time, slot.capacity) == (
        date(2026, 11, 11),
        time(14),
        time(16),
        10,
    )


async def test_calendar_marks_busy_day_and_hides_taken_or_past_time(context, bot):
    await day(context)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await open_wizard(dispatcher, bot)
    calendar = buttons(bot)
    assert "11•" in calendar and "12" in calendar
    await press(dispatcher, bot, calendar["10"])
    starts = buttons(bot)
    assert "10:00" not in starts and "10:30" in starts
    assert "Как в прошлый раз: 14:00–16:00 · мест: 3" in starts
    await press(dispatcher, bot, starts["← Назад"])
    assert "Выберите дату" in screen(bot).text
    await press(dispatcher, bot, buttons(bot)["11•"])
    starts = buttons(bot)
    assert "Уже есть: 14:00–16:00." in screen(bot).text
    assert not any(label.startswith("Как в прошлый раз") for label in starts)
    assert {"13:30", "16:00"} <= starts.keys()
    assert not {"14:00", "14:30", "15:00", "15:30"} & starts.keys()
    await press(dispatcher, bot, starts["13:00"])
    durations = {label for label in buttons(bot) if " · до " in label}
    assert durations == {"30 мин · до 13:30", "1 ч · до 14:00"}


async def test_repeat_previous_slot_on_another_day(context, bot):
    await day(context)
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await open_wizard(dispatcher, bot)
    await press(dispatcher, bot, buttons(bot)["12"])
    await press(dispatcher, bot, buttons(bot)["Как в прошлый раз: 14:00–16:00 · мест: 3"])
    assert "Дата: 12.11.2026 (Москва)\nВремя: 14:00–16:00\nМест: 3" in screen(bot).text
    await press(dispatcher, bot, buttons(bot)["Подтвердить"])
    created = (await slots(sessions))[-1]
    assert (created.date, created.start_time, created.end_time, created.capacity) == (
        date(2026, 11, 12),
        time(14),
        time(16),
        3,
    )


async def test_back_navigation_text_mix_paging_and_cancel(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await open_wizard(dispatcher, bot)
    await press(dispatcher, bot, buttons(bot)["11"])
    await dispatcher.feed_update(bot, message_update("09:15"))
    assert isinstance(screen(bot), SendMessage)
    await press(dispatcher, bot, buttons(bot)["1,5 ч · до 10:45"])
    assert "11.11.2026 (ср), 09:15–10:45" in screen(bot).text
    await press(dispatcher, bot, buttons(bot)["← Назад"])
    assert "начало 09:15" in screen(bot).text
    await press(dispatcher, bot, buttons(bot)["← Назад"])
    assert "Выберите время начала" in screen(bot).text
    await press(dispatcher, bot, buttons(bot)["← Назад"])
    await press(dispatcher, bot, buttons(bot)["Позже →"])
    assert "29 ноября – 12 декабря 2026" in screen(bot).text
    assert "1.12" in buttons(bot)
    await press(dispatcher, bot, buttons(bot)["← Раньше"])
    calendar = buttons(bot)
    await press(dispatcher, bot, calendar["Отмена"])
    assert screen(bot).text == "Создание слота защиты отменено."
    await press(dispatcher, bot, calendar["12"])
    assert "устарела" in bot.session.calls[-1].text
    assert await slots(sessions) == []


async def test_template_of_another_teacher_is_rejected(context, bot):
    sessions, ids, *_ = context
    async with sessions() as session:
        group = await create_group(session, actor_id=ids[303], name="Чужая")
    async with sessions() as session:
        await create_assignment(
            session,
            actor_id=ids[303],
            group_id=group.id,
            title="Чужая ЛР",
            description="Task",
            format=AssignmentFormat.ONLINE,
        )
    async with sessions() as session:
        foreign = await create_defense_slot(
            session,
            actor_id=ids[303],
            group_id=group.id,
            day_date=TODAY + timedelta(days=1),
            start_time=time(9),
            end_time=time(10),
            capacity=1,
        )
    dispatcher = create_dispatcher(sessions)
    await open_wizard(dispatcher, bot)
    await press(dispatcher, bot, buttons(bot)["12"])
    starts = buttons(bot)
    assert not any(label.startswith("Как в прошлый раз") for label in starts)
    nonce = TeacherSlotCallback.unpack(starts["10:30"]).nonce
    await press(
        dispatcher, bot, TeacherSlotCallback(action="repeat", value=foreign.id, nonce=nonce)
    )
    answer = bot.session.calls[-1]
    assert isinstance(answer, AnswerCallbackQuery) and answer.show_alert
    await press(dispatcher, bot, starts["10:30"])
    assert "начало 10:30" in screen(bot).text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(DefenseSlot)) == 1


async def test_failed_edit_falls_back_to_new_message(context, bot, monkeypatch):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await open_wizard(dispatcher, bot)
    original = bot.session.make_request

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="message can't be edited")
        return await original(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", request)
    await press(dispatcher, bot, buttons(bot)["11"])
    assert isinstance(screen(bot), SendMessage)
    assert "Дата: 11.11.2026 (ср)" in screen(bot).text


@pytest.mark.parametrize("value", [99999999, 20261109])
async def test_invalid_or_past_date_button_is_rejected(context, bot, value):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await open_wizard(dispatcher, bot)
    nonce = TeacherSlotCallback.unpack(buttons(bot)["11"]).nonce
    await press(dispatcher, bot, TeacherSlotCallback(action="date", value=value, nonce=nonce))
    assert "YYYY-MM-DD" in bot.session.calls[-1].text
    await press(dispatcher, bot, buttons(bot)["11"])
    assert "Дата: 11.11.2026" in screen(bot).text
