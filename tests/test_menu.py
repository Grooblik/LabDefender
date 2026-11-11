import pytest
import test_queue
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendMessage
from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy import func, select
from test_queue import message_update, press

from labdefender.app import create_dispatcher
from labdefender.config import Settings
from labdefender.db.models import StudyGroup, TeacherRegistrationAttempt, User
from labdefender.enums import Role
from labdefender.keyboards.groups import StudentGroupCallback, TeacherGroupCallback
from labdefender.keyboards.menu import (
    MENU_COMMANDS,
    MENU_LAYOUT,
    MenuButton,
    MenuCallback,
    main_menu,
)

# Teacher 101 owns «ИВТ-101» with students 202-204; teacher 303 and student 404 have no groups.
context = test_queue.context
TEACHER, STUDENT = 101, 202
CODE = "код-преподавателя"


def settings(*admins):
    return Settings(
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        teacher_registration_code=CODE,
        admin_ids=frozenset(admins),
        _env_file=None,
    )


def sent(bot):
    return [call for call in bot.session.calls if isinstance(call, SendMessage)]


def labels(message):
    return [button.text for row in message.reply_markup.inline_keyboard for button in row]


def button(message, text):
    return next(
        item.callback_data
        for row in message.reply_markup.inline_keyboard
        for item in row
        if item.text == text
    )


def menu_rows(message):
    assert isinstance(message.reply_markup, ReplyKeyboardMarkup)
    return [[item.text for item in row] for row in message.reply_markup.keyboard]


def layout(role):
    return [[item.value for item in row] for row in MENU_LAYOUT[role]]


def storage_key(bot, user):
    return StorageKey(bot_id=bot.id, chat_id=user, user_id=user)


def test_menu_layouts_and_commands_cover_every_button():
    assert layout(Role.STUDENT) == [
        ["📚 Задания", "🗓 Защита и очередь"],
        ["👥 Мои группы", "❓ Помощь"],
    ]
    assert layout(Role.TEACHER) == [
        ["📥 На проверку", "🗓 Защита и очередь"],
        ["📝 Новое задание", "🕒 Новый слот"],
        ["👥 Мои группы", "🔗 Пригласить"],
        ["🔔 Уведомления", "❓ Помощь"],
    ]
    assert set(MENU_COMMANDS) == set(MenuButton)
    keyboard = main_menu(Role.TEACHER)
    assert keyboard.is_persistent is True and keyboard.resize_keyboard is True
    for action in ("cancel", "id", "notifications", "grant"):
        assert len(MenuCallback(action=action).pack().encode()) <= 64


@pytest.mark.parametrize("user,role", [(STUDENT, Role.STUDENT), (TEACHER, Role.TEACHER)])
async def test_start_shows_role_menu(context, bot, user, role):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/start", user_id=user))
    [welcome] = sent(bot)
    assert menu_rows(welcome) == layout(role)
    assert ("«🎓 Я преподаватель»" in welcome.text) is (role == Role.STUDENT)


@pytest.mark.parametrize(
    "user,role,text",
    [
        (user, role, item.value)
        for user, role in [(STUDENT, Role.STUDENT), (TEACHER, Role.TEACHER)]
        for row in MENU_LAYOUT[role]
        for item in row
    ],
)
async def test_every_button_opens_the_same_screen_as_its_command(context, bot, user, role, text):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(
        bot, message_update(f"/{MENU_COMMANDS[MenuButton(text)]}", user_id=user)
    )
    by_command = sent(bot)[-1]
    await dispatcher.feed_update(bot, message_update(text, user_id=user))
    by_button = sent(bot)[-1]
    assert "недоступ" not in by_button.text
    assert (by_button.text, by_button.reply_markup) == (by_command.text, by_command.reply_markup)


async def test_section_button_leaves_a_text_step_without_using_it_as_input(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/create_group"))
    await dispatcher.feed_update(bot, message_update("🗓 Защита и очередь"))
    assert await dispatcher.storage.get_state(storage_key(bot, TEACHER)) is None
    assert sent(bot)[-1].text == "Выберите группу:"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 1


async def test_section_button_is_never_counted_as_a_teacher_code(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("/teacher", user_id=404))
    assert labels(sent(bot)[-1]) == ["Отмена"]
    await dispatcher.feed_update(bot, message_update("📚 Задания", user_id=404))
    assert await dispatcher.storage.get_state(storage_key(bot, 404)) is None
    assert not any(isinstance(call, DeleteMessage) for call in bot.session.calls)
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TeacherRegistrationAttempt)) == 0
        )


async def test_teacher_code_success_switches_to_teacher_menu(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions, settings=settings())
    await dispatcher.feed_update(bot, message_update("❓ Помощь", user_id=404))
    await press(dispatcher, bot, button(sent(bot)[-1], "🎓 Я преподаватель"), user=404)
    await dispatcher.feed_update(bot, message_update(CODE, user_id=404))
    assert "Доступ преподавателя открыт" in sent(bot)[-1].text
    assert menu_rows(sent(bot)[-1]) == layout(Role.TEACHER)


@pytest.mark.parametrize(
    "user,role,text",
    [(STUDENT, Role.STUDENT, "📥 На проверку"), (TEACHER, Role.TEACHER, "📚 Задания")],
)
async def test_button_of_another_role_refreshes_menu(context, bot, user, role, text):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update(text, user_id=user))
    [answer] = sent(bot)
    assert answer.text == "Этот раздел недоступен для вашей роли. Меню обновлено."
    assert menu_rows(answer) == layout(role)


async def test_cancel_button_under_prompt_works_like_cancel_command(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/create_group"))
    prompt = sent(bot)[-1]
    assert prompt.text == "Введите название группы (до 128 символов)."
    await press(dispatcher, bot, button(prompt, "Отмена"))
    assert await dispatcher.storage.get_state(storage_key(bot, TEACHER)) is None
    assert sent(bot)[-1].text == "Ввод отменён. Сохранённые данные не изменены."
    assert menu_rows(sent(bot)[-1]) == layout(Role.TEACHER)
    await press(dispatcher, bot, button(prompt, "Отмена"))
    assert bot.session.calls[-1].text == "Сейчас нечего отменять."
    await dispatcher.feed_update(bot, message_update("Новая группа"))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 1


async def test_help_lists_sections_with_commands_and_role_buttons(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions, settings=settings(TEACHER))
    await dispatcher.feed_update(bot, message_update("❓ Помощь", user_id=STUDENT))
    student_help = sent(bot)[-1]
    assert "📚 Задания — /assignments" in student_help.text
    assert "/grant_teacher" not in student_help.text
    assert labels(student_help) == ["🆔 Мой Telegram ID", "🔔 Уведомления", "🎓 Я преподаватель"]
    await press(dispatcher, bot, button(student_help, "🆔 Мой Telegram ID"), user=STUDENT)
    assert sent(bot)[-1].text == "Ваш Telegram ID: 202"
    await press(dispatcher, bot, button(student_help, "🔔 Уведомления"), user=STUDENT)
    assert "Студентам уведомления приходят всегда" in sent(bot)[-1].text

    await dispatcher.feed_update(bot, message_update("/help"))
    teacher_help = sent(bot)[-1]
    assert "📥 На проверку — /submissions" in teacher_help.text
    assert "/grant_teacher" in teacher_help.text
    assert labels(teacher_help) == [
        "🆔 Мой Telegram ID",
        "➕ Создать группу",
        "👤 Выдать роль преподавателя",
    ]


async def test_admin_grants_teacher_role_with_buttons(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions, settings=settings(TEACHER))
    await dispatcher.feed_update(bot, message_update("/help"))
    grant = button(sent(bot)[-1], "👤 Выдать роль преподавателя")
    await press(dispatcher, bot, grant, user=STUDENT)
    assert bot.session.calls[-1].show_alert is True
    assert "Недостаточно прав" in bot.session.calls[-1].text

    await press(dispatcher, bot, grant)
    assert "Отправьте Telegram ID пользователя" in sent(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("abc"))
    assert "Нужен числовой Telegram ID" in sent(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("999"))
    assert "Пользователь не зарегистрирован" in sent(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("404"))
    assert sent(bot)[-1].text == "Роль преподавателя выдана."
    assert await dispatcher.storage.get_state(storage_key(bot, TEACHER)) is None
    async with sessions() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 404))
        assert user.role == Role.TEACHER

    # The new teacher still has the student keyboard: any of its buttons refreshes it.
    await dispatcher.feed_update(bot, message_update("📚 Задания", user_id=404))
    assert menu_rows(sent(bot)[-1]) == layout(Role.TEACHER)

    await dispatcher.feed_update(bot, message_update("/grant_teacher"))
    assert "Отправьте Telegram ID пользователя" in sent(bot)[-1].text
    await dispatcher.feed_update(bot, message_update("203"))
    assert sent(bot)[-1].text == "Роль преподавателя выдана."


async def test_group_creation_and_empty_states_have_buttons(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("👥 Мои группы", user_id=404))
    assert sent(bot)[-1].text.startswith("Вы пока не состоите в группах")
    await dispatcher.feed_update(bot, message_update("📚 Задания", user_id=404))
    assert sent(bot)[-1].text.startswith("Вы пока не состоите в группах")

    await dispatcher.feed_update(bot, message_update("🔗 Пригласить", user_id=303))
    empty = sent(bot)[-1]
    assert empty.text == "У вас пока нет групп. Создайте первую кнопкой ниже."
    await press(dispatcher, bot, button(empty, "➕ Создать группу"), user=303)
    await dispatcher.feed_update(bot, message_update("ПМИ-201", user_id=303))
    await press(dispatcher, bot, button(sent(bot)[-1], "Создать"), user=303)
    async with sessions() as session:
        assert await session.scalar(select(StudyGroup.name).order_by(StudyGroup.id.desc())) == (
            "ПМИ-201"
        )
    await dispatcher.feed_update(bot, message_update("👥 Мои группы", user_id=303))
    assert labels(sent(bot)[-1])[-1] == "➕ Создать группу"


async def test_group_cards_lead_to_defense_and_queue(context, bot):
    sessions, _, group, *_ = context
    dispatcher = create_dispatcher(sessions)
    await press(dispatcher, bot, TeacherGroupCallback(action="open", value=group.id))
    card = sent(bot)[-1]
    assert "Слоты защиты и очередь" in labels(card)
    await press(dispatcher, bot, button(card, "Слоты защиты и очередь"))
    assert sent(bot)[-1].text == "Группа «ИВТ-101».\nВыберите задание:"

    await press(dispatcher, bot, StudentGroupCallback(action="open", value=group.id), user=STUDENT)
    card = sent(bot)[-1]
    assert labels(card) == ["Задания группы", "Защита и очередь"]
    await press(dispatcher, bot, button(card, "Защита и очередь"), user=STUDENT)
    assert sent(bot)[-1].text == "Группа «ИВТ-101».\nВыберите задание:"


async def test_unknown_text_restores_menu(context, bot):
    sessions, *_ = context
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("привет", user_id=STUDENT))
    answer = sent(bot)[-1]
    assert "Выберите раздел кнопками ниже" in answer.text
    assert menu_rows(answer) == layout(Role.STUDENT)
    await press(dispatcher, bot, "menu:unknown", user=STUDENT)
    assert isinstance(bot.session.calls[-1], AnswerCallbackQuery)
