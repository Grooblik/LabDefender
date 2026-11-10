import secrets
from dataclasses import asdict

from aiogram import F, Router
from aiogram.filters import Command
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
    show_student_assignment,
    show_submission,
    show_submissions,
)
from labdefender.keyboards.coursework import (
    StudentCourseCallback,
    StudentDraftCallback,
    draft_keyboard,
    selection_keyboard,
)
from labdefender.services.assignments import get_assignment
from labdefender.services.errors import InvalidSolution, StaleCourseAction, SubmissionConflict
from labdefender.services.submissions import (
    Solution,
    document_solution,
    latest_submission,
    submit_solution,
    text_solution,
)
from labdefender.states.coursework import SolutionSubmission


def create_router() -> Router:
    router = Router(name="student_coursework")

    @router.message(Command("assignments"))
    async def assignments(
        message: Message, session: AsyncSession, current_user: User, state: FSMContext
    ) -> None:
        await state.clear()
        await show_course_groups(message, session, current_user, mode="student")

    @router.callback_query(StudentCourseCallback.filter())
    async def course_action(
        callback: CallbackQuery,
        callback_data: StudentCourseCallback,
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
        if action == "groups":
            await show_course_groups(
                callback.message, session, current_user, mode="student", offset=offset
            )
        elif action == "list":
            await show_assignments(
                callback.message, session, current_user, group_id=object_id, offset=offset
            )
        elif action == "open":
            await show_student_assignment(
                callback.message, session, current_user, assignment_id=object_id
            )
        elif action == "submit":
            assignment = await get_assignment(
                session, actor_id=current_user.id, assignment_id=object_id
            )
            latest = await latest_submission(
                session, actor_id=current_user.id, assignment_id=object_id
            )
            if latest is not None and latest.status != SubmissionStatus.RETURNED:
                raise SubmissionConflict()
            await state.set_data(
                {"assignment_id": assignment.id, "previous_id": latest.id if latest else None}
            )
            await state.set_state(SolutionSubmission.payload)
            await callback.message.answer(
                f"Задание: {assignment.title}\n"
                "Отправьте один PDF/ZIP до 20 МиБ, текст или ссылку HTTP(S). Отмена: /cancel."
            )
        elif action == "history":
            await show_submissions(
                callback.message, session, current_user, assignment_id=object_id, offset=offset
            )
        elif action == "attempt":
            await show_submission(callback.message, session, current_user, submission_id=object_id)
        await callback.answer()

    @router.message(SolutionSubmission.payload, F.document)
    @router.message(SolutionSubmission.payload, PlainTextInput())
    async def solution_payload(message: Message, state: FSMContext) -> None:
        if message.media_group_id is not None:
            raise InvalidSolution()
        if message.document:
            document = message.document
            solution = document_solution(
                file_id=document.file_id,
                file_name=document.file_name,
                file_size=document.file_size,
                mime_type=document.mime_type,
            )
            preview = f"Файл: {document.file_name}\nРазмер: {document.file_size} байт"
        elif message.text:
            solution = text_solution(message.text)
            preview = solution.content
        else:
            raise InvalidSolution()
        nonce = secrets.token_hex(8)
        await state.update_data(solution=asdict(solution), nonce=nonce)
        await state.set_state(SolutionSubmission.confirmation)
        await send_text(
            message,
            f"Отправить решение?\n\n{preview}",
            reply_markup=draft_keyboard(nonce, teacher=False),
        )

    @router.callback_query(StudentDraftCallback.filter())
    async def confirm_solution(
        callback: CallbackQuery,
        callback_data: StudentDraftCallback,
        session: AsyncSession,
        current_user: User,
        state: FSMContext,
    ) -> None:
        if not isinstance(callback.message, Message):
            raise StaleCourseAction()
        data = await require_draft(
            state, expected_state=SolutionSubmission.confirmation.state, nonce=callback_data.nonce
        )
        if callback_data.action == "cancel":
            await state.clear()
            await callback.message.answer("Отправка решения отменена.")
        else:
            submission = await submit_solution(
                session,
                actor_id=current_user.id,
                assignment_id=data["assignment_id"],
                expected_previous_id=data["previous_id"],
                solution=Solution(**data["solution"]),
            )
            await state.clear()
            await callback.message.answer(
                f"Попытка №{submission.attempt_number} отправлена на проверку.",
                reply_markup=selection_keyboard(
                    [
                        (
                            "Статус задания",
                            StudentCourseCallback(
                                action="open", object_id=submission.assignment_id
                            ).pack(),
                        )
                    ]
                ),
            )
        await callback.answer()

    @router.message(SolutionSubmission.payload, ~F.text)
    async def unsupported_solution(message: Message) -> None:
        raise InvalidSolution()

    return router
