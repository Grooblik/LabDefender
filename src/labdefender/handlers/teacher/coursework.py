import secrets

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import SubmissionStatus
from labdefender.filters.plain_text import PlainTextInput
from labdefender.handlers.coursework import (
    require_draft,
    send_text,
    show_assignments,
    show_course_groups,
    show_submission,
    show_submissions,
)
from labdefender.keyboards.coursework import (
    TeacherCourseCallback,
    TeacherDraftCallback,
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
            await state.set_state(AssignmentCreation.title)
            await callback.message.answer(
                f"Группа: {group.name}\n"
                "Введите название задания (до 256 символов). Отмена: /cancel."
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
            await callback.message.answer(
                "Работа допущена. Студент увидит статус в карточке задания."
            )
        elif action == "return":
            await require_reviewable(session, actor_id=current_user.id, submission_id=object_id)
            await state.set_data({"submission_id": object_id})
            await state.set_state(SubmissionReview.comment)
            await callback.message.answer(
                "Введите комментарий для возврата (до 4000 символов). Отмена: /cancel."
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
        group = await require_group_owner(
            session, actor_id=current_user.id, group_id=data["group_id"]
        )
        nonce = secrets.token_hex(8)
        await state.update_data(description=description, nonce=nonce)
        await state.set_state(AssignmentCreation.confirmation)
        await send_text(
            message,
            f"Опубликовать в группе «{group.name}»?\n{title}\n\n{description}",
            reply_markup=draft_keyboard(nonce, teacher=True),
        )

    @router.callback_query(TeacherDraftCallback.filter())
    async def publish_assignment(
        callback: CallbackQuery,
        callback_data: TeacherDraftCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
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
            )
            await state.clear()
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
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
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
        await message.answer("Работа возвращена на доработку. Комментарий доступен студенту.")

    @router.message(
        StateFilter(
            AssignmentCreation.title, AssignmentCreation.description, SubmissionReview.comment
        ),
        ~F.text,
    )
    async def text_required(message: Message) -> None:
        await message.answer("Отправьте текстовое сообщение или отмените ввод: /cancel.")

    return router
