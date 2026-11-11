import secrets

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import AssignmentFormat, SubmissionStatus
from labdefender.filters.plain_text import PlainTextInput
from labdefender.handlers.coursework import (
    require_draft,
    send_text,
    show_assignments,
    show_course_groups,
    show_submission,
    show_submissions,
)
from labdefender.handlers.notifications import (
    Notifier,
    notify_assignment_published,
    notify_submission_reviewed,
)
from labdefender.keyboards.coursework import (
    AssignmentFormatCallback,
    TeacherCourseCallback,
    TeacherDraftCallback,
    assignment_format_keyboard,
    draft_keyboard,
    selection_keyboard,
)
from labdefender.services.access import require_group_owner
from labdefender.services.assignments import create_assignment, validate_assignment
from labdefender.services.errors import InvalidAssignment, StaleCourseAction
from labdefender.services.submissions import require_reviewable, review_submission
from labdefender.states.coursework import AssignmentCreation, SubmissionReview


def create_router() -> Router:
    router = Router(name="teacher_coursework")

    @router.message(Command("new_assignment"))
    async def new_assignment(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_course_groups(message, session, current_user, mode="publish")

    @router.message(Command("submissions"))
    async def submissions(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_course_groups(message, session, current_user, mode="review")

    @router.callback_query(TeacherCourseCallback.filter())
    async def course_action(
        callback: CallbackQuery,
        callback_data: TeacherCourseCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        bot: Bot,
        notifier: Notifier,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        action, object_id, offset = (
            callback_data.action,
            callback_data.object_id,
            callback_data.offset,
        )
        await state.clear()
        if action in {"groups_publish", "groups_review"}:
            await show_course_groups(
                callback.message,
                session,
                current_user,
                mode="publish" if action == "groups_publish" else "review",
                offset=offset,
            )
        elif action == "publish":
            group = await require_group_owner(session, actor_id=current_user.id, group_id=object_id)
            await state.set_data({"group_id": group.id})
            await state.set_state(AssignmentCreation.format)
            await callback.message.answer(
                f"Группа: {group.name}\nВыберите формат сдачи задания.",
                reply_markup=assignment_format_keyboard(),
            )
        elif action == "review_list":
            await show_assignments(
                callback.message, session, current_user, group_id=object_id, offset=offset
            )
        elif action == "attempts":
            await show_submissions(
                callback.message, session, current_user, assignment_id=object_id, offset=offset
            )
        elif action == "open":
            await show_submission(callback.message, session, current_user, submission_id=object_id)
        elif action == "approve":
            await review_submission(
                session,
                actor_id=current_user.id,
                submission_id=object_id,
                decision=SubmissionStatus.APPROVED,
            )
            await notify_submission_reviewed(notifier, bot, session, submission_id=object_id)
            await callback.message.answer(
                "Работа допущена. Студенту отправлено уведомление, статус есть в карточке задания."
            )
        elif action == "return":
            await require_reviewable(session, actor_id=current_user.id, submission_id=object_id)
            await state.set_data({"submission_id": object_id})
            await state.set_state(SubmissionReview.comment)
            await callback.message.answer(
                "Введите комментарий для возврата (до 4000 символов). Отмена: /cancel."
            )
        await callback.answer()

    @router.callback_query(AssignmentFormatCallback.filter())
    async def choose_format(
        callback: CallbackQuery,
        callback_data: AssignmentFormatCallback,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        if await state.get_state() != AssignmentCreation.format.state:
            raise StaleCourseAction()
        if callback_data.action == "cancel":
            await state.clear()
            await callback.message.answer("Создание задания отменено.")
        else:
            assignment_format = (
                AssignmentFormat.IN_PERSON
                if callback_data.action == "in_person"
                else AssignmentFormat.ONLINE
            )
            await state.update_data(format=assignment_format.value)
            await state.set_state(AssignmentCreation.title)
            await callback.message.answer(
                "Введите название задания (до 256 символов). Отмена: /cancel."
            )
        await callback.answer()

    @router.message(AssignmentCreation.title, PlainTextInput())
    async def assignment_title(message: Message, state: FSMContext) -> None:
        title = " ".join(message.text.split())
        if not 1 <= len(title) <= 256:
            raise InvalidAssignment()
        await state.update_data(title=title)
        await state.set_state(AssignmentCreation.description)
        await message.answer("Введите условие задания (до 4000 символов). Отмена: /cancel.")

    @router.message(AssignmentCreation.description, PlainTextInput())
    async def assignment_description(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        data = await state.get_data()
        title, description = validate_assignment(data["title"], message.text)
        assignment_format = AssignmentFormat(data["format"])
        group = await require_group_owner(
            session, actor_id=current_user.id, group_id=data["group_id"]
        )
        nonce = secrets.token_hex(8)
        await state.update_data(description=description, nonce=nonce)
        await state.set_state(AssignmentCreation.confirmation)
        format_label = "очная" if assignment_format == AssignmentFormat.IN_PERSON else "онлайн"
        await send_text(
            message,
            f"Опубликовать в группе «{group.name}»?\n"
            f"Формат сдачи: {format_label}\n{title}\n\n{description}",
            reply_markup=draft_keyboard(nonce, teacher=True),
        )

    @router.callback_query(TeacherDraftCallback.filter())
    async def publish_assignment(
        callback: CallbackQuery,
        callback_data: TeacherDraftCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        bot: Bot,
        notifier: Notifier,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        data = await require_draft(
            state, expected_state=AssignmentCreation.confirmation.state, nonce=callback_data.nonce
        )
        if callback_data.action == "cancel":
            await state.clear()
            await callback.message.answer("Публикация отменена.")
        else:
            assignment = await create_assignment(
                session,
                actor_id=current_user.id,
                group_id=data["group_id"],
                title=data["title"],
                description=data["description"],
                format=data["format"],
            )
            await state.clear()
            await notify_assignment_published(notifier, bot, session, assignment_id=assignment.id)
            await callback.message.answer(
                f"Задание «{assignment.title}» опубликовано.",
                reply_markup=selection_keyboard(
                    [
                        (
                            "Попытки задания",
                            TeacherCourseCallback(
                                action="attempts", object_id=assignment.id
                            ).pack(),
                        )
                    ]
                ),
            )
        await callback.answer()

    @router.message(SubmissionReview.comment, PlainTextInput())
    async def return_comment(
        message: Message,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
        bot: Bot,
        notifier: Notifier,
    ) -> None:
        data = await state.get_data()
        await review_submission(
            session,
            actor_id=current_user.id,
            submission_id=data["submission_id"],
            decision=SubmissionStatus.RETURNED,
            comment=message.text,
        )
        await state.clear()
        await notify_submission_reviewed(
            notifier, bot, session, submission_id=data["submission_id"]
        )
        await message.answer(
            "Работа возвращена на доработку. Студенту отправлено уведомление с комментарием."
        )

    @router.message(AssignmentCreation.format)
    async def format_requires_button(message: Message) -> None:
        await message.answer(
            "Выберите формат сдачи кнопкой выше или отмените ввод: /cancel.",
            reply_markup=assignment_format_keyboard(),
        )

    @router.message(
        StateFilter(
            AssignmentCreation.title,
            AssignmentCreation.description,
            SubmissionReview.comment,
        ),
        ~F.text,
    )
    async def text_required(message: Message) -> None:
        await message.answer("Отправьте текстовое сообщение или отмените ввод: /cancel.")

    return router
