from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.handlers import menu, registration
from labdefender.handlers.notifications import (
    Notifier,
    notify_student_joined,
    show_notification_settings,
)
from labdefender.handlers.student.groups import show_student_group
from labdefender.keyboards.menu import (
    MenuButton,
    MenuCallback,
    app_keyboard,
    help_keyboard,
    main_menu,
    menu_sections,
)
from labdefender.services.groups import join_group, parse_invite_payload

CANCELLED = "Ввод отменён. Сохранённые данные не изменены."


APP_DISABLED = "Приложение сейчас выключено: бот запущен без Mini App. Все разделы есть в чате."
APP_INVITATION = (
    "Всё то же самое есть в приложении: задания, проверка, слоты и очередь — в одном окне. "
    "Файлы PDF/ZIP по-прежнему отправляются в этот чат."
)


def help_text(user: User, *, admin: bool, app: bool = False) -> str:
    extra = (
        ["/notifications — уведомления", "/teacher — получить доступ преподавателя"]
        if user.role == Role.STUDENT
        else ["/create_group — создать группу"]
    )
    if app:
        extra.insert(0, "/app — открыть приложение")
    if admin:
        extra.append("/grant_teacher <telegram_id> — выдать роль преподавателя")
    return "\n".join(
        [
            "LabDefender — сдача и защита лабораторных работ.",
            "",
            "Разделы открываются кнопками под полем ввода, действия — кнопками под "
            "сообщениями бота. Те же разделы доступны командами:",
            *menu_sections(user.role),
            "",
            "/start — главное меню",
            "/cancel — отменить текущий ввод",
            "/id — мой Telegram ID",
            "/name — изменить своё имя",
            *extra,
        ]
    )


def create_router() -> Router:
    router = Router(name="common")

    @router.message(CommandStart())
    async def start(
        message: Message,
        command: CommandObject,
        current_user: User,
        state: FSMContext,
        session: AsyncSession,
        bot: Bot,
        notifier: Notifier,
        webapp_url: str,
    ) -> None:
        await state.clear()
        if command.args:
            result = await join_group(
                session, actor_id=current_user.id, invite_token=parse_invite_payload(command.args)
            )
            if result.joined:
                await notify_student_joined(
                    notifier, bot, session, group_id=result.group.id, student_id=current_user.id
                )
            title = "Вы присоединились к группе" if result.joined else "Вы уже в группе"
            # The invite is often the first contact: show the menu, then the group itself.
            await message.answer(
                f"{title} «{result.group.name}».", reply_markup=main_menu(current_user.role)
            )
            await show_student_group(message, session, current_user, group_id=result.group.id)
            return
        teacher = current_user.role == Role.TEACHER
        role = "преподаватель" if teacher else "студент"
        await message.answer(
            f"Добро пожаловать в LabDefender!\nВаша роль: {role}.\n"
            "Разделы — на кнопках под полем ввода, действия — на кнопках под сообщениями. "
            f"Справка: «{MenuButton.HELP.value}»."
            + ("" if teacher else "\nВы преподаватель? «❓ Помощь» → «🎓 Я преподаватель»."),
            reply_markup=main_menu(current_user.role),
        )
        # One message carries one keyboard: the menu above, the app button in its own message.
        if webapp_url:
            await message.answer(APP_INVITATION, reply_markup=app_keyboard(webapp_url))

    # Help keeps the current input: a code or a draft can be continued after reading it.
    @router.message(or_f(Command("help"), F.text == MenuButton.HELP.value))
    async def help_command(
        message: Message, current_user: User, admin_ids: frozenset[int], webapp_url: str
    ) -> None:
        admin = current_user.telegram_id in admin_ids
        await message.answer(
            help_text(current_user, admin=admin, app=bool(webapp_url)),
            reply_markup=help_keyboard(current_user.role, admin=admin, webapp_url=webapp_url),
        )

    @router.message(Command("app"))
    async def open_app(message: Message, webapp_url: str) -> None:
        if not webapp_url:
            await message.answer(APP_DISABLED)
            return
        await message.answer(
            "Приложение LabDefender откроется внутри Telegram.",
            reply_markup=app_keyboard(webapp_url),
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext, current_user: User) -> None:
        await state.clear()
        await message.answer(CANCELLED, reply_markup=main_menu(current_user.role))

    @router.message(Command("id"))
    async def my_id(message: Message, current_user: User) -> None:
        await message.answer(f"Ваш Telegram ID: {current_user.telegram_id}")

    @router.callback_query(MenuCallback.filter(F.action == "cancel"))
    async def cancel_button(callback: CallbackQuery, state: FSMContext, current_user: User) -> None:
        if await state.get_state() is None:
            await callback.answer("Сейчас нечего отменять.")
            return
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(CANCELLED, reply_markup=main_menu(current_user.role))
        await callback.answer()

    @router.callback_query(MenuCallback.filter(F.action == "id"))
    async def id_button(callback: CallbackQuery, current_user: User) -> None:
        if isinstance(callback.message, Message):
            await callback.message.answer(f"Ваш Telegram ID: {current_user.telegram_id}")
        await callback.answer()

    @router.callback_query(MenuCallback.filter(F.action == "notifications"))
    async def notifications_button(
        callback: CallbackQuery, session: AsyncSession, current_user: User
    ) -> None:
        if isinstance(callback.message, Message):
            await show_notification_settings(callback.message, session, current_user)
        await callback.answer()

    # Section buttons first: they must win over any text a dialogue step is waiting for.
    router.include_routers(menu.create_router(), registration.create_router())
    return router
