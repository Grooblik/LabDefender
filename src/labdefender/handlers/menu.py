"""Entry points to sections: a main-menu button and its command open the same screen.

The router runs before the role routers and before any dialogue step, so a section button or
a section command always leaves the current input, as /cancel does. Buttons of the other role
(a keyboard left over from before a role change) only refresh the menu.
"""

from aiogram import F, Router
from aiogram.filters import Command, or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.filters import RoleFilter
from labdefender.handlers.coursework import show_course_groups
from labdefender.handlers.defense import show_queue_groups
from labdefender.handlers.notifications import show_notification_settings
from labdefender.handlers.student import groups as student_groups
from labdefender.handlers.teacher import groups as teacher_groups
from labdefender.keyboards.menu import MENU_TEXTS, MenuButton, main_menu


def entry(button: MenuButton, *commands: str):
    return or_f(Command(*commands), F.text == button.value)


def create_router() -> Router:
    router = Router(name="menu")
    teacher = RoleFilter(Role.TEACHER)
    student = RoleFilter(Role.STUDENT)

    @router.message(entry(MenuButton.GROUPS, "my_groups"))
    async def my_groups(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        if current_user.role == Role.TEACHER:
            await teacher_groups.show_groups(message, session, current_user)
        else:
            await student_groups.show_groups(message, session, current_user)

    @router.message(entry(MenuButton.QUEUE, "queue"))
    async def queue(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_queue_groups(message, session, current_user)

    @router.message(entry(MenuButton.NOTIFICATIONS, "notifications"))
    async def notifications(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_notification_settings(message, session, current_user)

    @router.message(student, entry(MenuButton.ASSIGNMENTS, "assignments"))
    async def assignments(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_course_groups(message, session, current_user, mode="student")

    @router.message(teacher, entry(MenuButton.REVIEW, "submissions"))
    async def submissions(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_course_groups(message, session, current_user, mode="review")

    @router.message(teacher, entry(MenuButton.NEW_ASSIGNMENT, "new_assignment"))
    async def new_assignment(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_course_groups(message, session, current_user, mode="publish")

    @router.message(teacher, entry(MenuButton.NEW_SLOT, "new_slot", "new_defense_day"))
    async def new_slot(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_queue_groups(message, session, current_user, new=True)

    @router.message(teacher, entry(MenuButton.INVITE, "invite"))
    async def invite(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await teacher_groups.show_groups(message, session, current_user, invite=True)

    @router.message(teacher, Command("create_group"))
    async def create_group(message: Message, state: FSMContext) -> None:
        await teacher_groups.start_group_creation(message, state)

    @router.message(F.text.in_(MENU_TEXTS))
    async def other_role_button(message: Message, current_user: User, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Этот раздел недоступен для вашей роли. Меню обновлено.",
            reply_markup=main_menu(current_user.role),
        )

    return router
