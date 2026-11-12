from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import Assignment, StudyGroup, Submission, User
from labdefender.db.transactions import immediate_transaction
from labdefender.enums import Role, SubmissionKind, SubmissionStatus
from labdefender.services.access import (
    require_assignment_access,
    require_submission_access,
    require_user,
)
from labdefender.services.assignments import text_units
from labdefender.services.errors import InvalidReviewComment, InvalidSolution, SubmissionConflict
from labdefender.services.pagination import PAGE_SIZE, Page, validate_offset

MAX_FILE_SIZE = 20 * 1024 * 1024
STATUS_LABELS = {
    SubmissionStatus.PENDING: "На проверке",
    SubmissionStatus.RETURNED: "На доработке",
    SubmissionStatus.APPROVED: "Допущено",
}


@dataclass(frozen=True)
class Solution:
    kind: SubmissionKind
    content: str | None = None
    file_id: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None


def validate_solution(solution: Solution) -> Solution:
    if solution.kind in {SubmissionKind.PDF, SubmissionKind.ZIP}:
        extension = ".pdf" if solution.kind == SubmissionKind.PDF else ".zip"
        accepted_mime = (
            {"application/octet-stream", "application/pdf"}
            if extension == ".pdf"
            else {"application/octet-stream", "application/zip", "application/x-zip-compressed"}
        )
        if (
            not solution.file_id
            or solution.content is not None
            or not solution.file_name
            or PurePath(solution.file_name).suffix.lower() != extension
            or solution.file_size is None
            or not 0 < solution.file_size <= MAX_FILE_SIZE
            or (solution.mime_type is not None and solution.mime_type.lower() not in accepted_mime)
        ):
            raise InvalidSolution()
    elif solution.kind in {SubmissionKind.TEXT, SubmissionKind.LINK}:
        if (
            not solution.content
            or not solution.content.strip()
            or text_units(solution.content) > 4096
            or solution.file_id is not None
        ):
            raise InvalidSolution()
        if solution.kind == SubmissionKind.LINK:
            try:
                url = urlsplit(solution.content)
                valid = url.scheme.lower() in {"http", "https"} and bool(url.hostname)
                valid = valid and not any(
                    character.isspace() or ord(character) < 32 for character in solution.content
                )
                _ = url.port  # Validate malformed ports as well.
            except ValueError:
                valid = False
            if not valid:
                raise InvalidSolution()
    else:
        raise InvalidSolution()
    return solution


def text_solution(text: str) -> Solution:
    trimmed = text.strip()
    kind = (
        SubmissionKind.LINK
        if trimmed.lower().startswith(("http://", "https://"))
        else SubmissionKind.TEXT
    )
    # Preserve indentation and trailing newlines in submitted code/text.
    return validate_solution(
        Solution(kind=kind, content=trimmed if kind == SubmissionKind.LINK else text)
    )


def document_solution(
    *, file_id: str, file_name: str | None, file_size: int | None, mime_type: str | None
) -> Solution:
    extension = PurePath(file_name or "").suffix.lower()
    if extension not in {".pdf", ".zip"}:
        raise InvalidSolution()
    return validate_solution(
        Solution(
            kind=SubmissionKind.PDF if extension == ".pdf" else SubmissionKind.ZIP,
            file_id=file_id,
            file_name=file_name,
            file_size=file_size,
            mime_type=mime_type,
        )
    )


async def _latest(
    session: AsyncSession, *, student_id: int, assignment_id: int
) -> Submission | None:
    return await session.scalar(
        select(Submission)
        .where(Submission.student_id == student_id, Submission.assignment_id == assignment_id)
        .order_by(Submission.attempt_number.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


async def latest_submission(
    session: AsyncSession, *, actor_id: int, assignment_id: int
) -> Submission | None:
    await require_user(session, actor_id=actor_id, role=Role.STUDENT)
    await require_assignment_access(session, actor_id=actor_id, assignment_id=assignment_id)
    return await _latest(session, student_id=actor_id, assignment_id=assignment_id)


async def submit_solution(
    session: AsyncSession,
    *,
    actor_id: int,
    assignment_id: int,
    expected_previous_id: int | None,
    solution: Solution,
) -> Submission:
    validate_solution(solution)
    async with immediate_transaction(session):
        await require_user(session, actor_id=actor_id, role=Role.STUDENT)
        await require_assignment_access(session, actor_id=actor_id, assignment_id=assignment_id)
        latest = await _latest(session, student_id=actor_id, assignment_id=assignment_id)
        if (latest.id if latest else None) != expected_previous_id or (
            latest is not None and latest.status != SubmissionStatus.RETURNED
        ):
            raise SubmissionConflict()
        submission = Submission(
            assignment_id=assignment_id,
            student_id=actor_id,
            attempt_number=latest.attempt_number + 1 if latest else 1,
            kind=solution.kind,
            content=solution.content,
            file_id=solution.file_id,
            status=SubmissionStatus.PENDING,
        )
        session.add(submission)
        await session.flush()
    return submission


async def require_reviewable(
    session: AsyncSession, *, actor_id: int, submission_id: int
) -> Submission:
    await require_user(session, actor_id=actor_id, role=Role.TEACHER)
    submission = await require_submission_access(
        session, actor_id=actor_id, submission_id=submission_id
    )
    latest = await _latest(
        session, student_id=submission.student_id, assignment_id=submission.assignment_id
    )
    if (
        latest is None
        or submission.status != SubmissionStatus.PENDING
        or latest.id != submission.id
    ):
        raise SubmissionConflict()
    return submission


async def review_submission(
    session: AsyncSession,
    *,
    actor_id: int,
    submission_id: int,
    decision: SubmissionStatus,
    comment: str | None = None,
) -> Submission:
    if decision not in {SubmissionStatus.RETURNED, SubmissionStatus.APPROVED}:
        raise SubmissionConflict()
    if decision == SubmissionStatus.RETURNED:
        comment = (comment or "").strip()
        if not 1 <= text_units(comment) <= 4000:
            raise InvalidReviewComment()
    async with immediate_transaction(session):
        submission = await require_reviewable(
            session, actor_id=actor_id, submission_id=submission_id
        )
        submission.status = decision
        submission.review_comment = comment if decision == SubmissionStatus.RETURNED else None
        submission.approved_at = (
            datetime.now(UTC).replace(tzinfo=None)
            if decision == SubmissionStatus.APPROVED
            else None
        )
    return submission


@dataclass(frozen=True)
class SubmissionItem:
    submission: Submission
    student_name: str


@dataclass(frozen=True)
class SubmissionDetail:
    submission: Submission
    student: User
    assignment: Assignment
    group: StudyGroup


async def list_submissions(
    session: AsyncSession, *, actor_id: int, assignment_id: int, offset: int = 0
) -> Page[SubmissionItem]:
    validate_offset(offset)
    user = await require_user(session, actor_id=actor_id)
    await require_assignment_access(session, actor_id=actor_id, assignment_id=assignment_id)
    query = (
        select(Submission, User.full_name)
        .join(User, User.id == Submission.student_id)
        .where(Submission.assignment_id == assignment_id)
    )
    if user.role == Role.STUDENT:
        query = query.where(Submission.student_id == actor_id)
    rows = (
        await session.execute(
            query.order_by(Submission.id.desc()).offset(offset).limit(PAGE_SIZE + 1)
        )
    ).all()
    return Page(
        items=tuple(SubmissionItem(*row) for row in rows[:PAGE_SIZE]),
        offset=offset,
        has_next=len(rows) > PAGE_SIZE,
    )


async def get_submission(
    session: AsyncSession, *, actor_id: int, submission_id: int
) -> SubmissionDetail:
    submission = await require_submission_access(
        session, actor_id=actor_id, submission_id=submission_id
    )
    assignment = await session.get(Assignment, submission.assignment_id)
    return SubmissionDetail(
        submission=submission,
        student=await session.get(User, submission.student_id),
        assignment=assignment,
        group=await session.get(StudyGroup, assignment.group_id),
    )


@dataclass(frozen=True)
class PendingReview:
    submission: Submission
    student_name: str
    assignment: Assignment
    group_name: str


async def list_pending_reviews(
    session: AsyncSession, *, actor_id: int, offset: int = 0
) -> Page[PendingReview]:
    """Attempts awaiting review in all of the teacher's groups, oldest first.

    A pending attempt is always the student's latest one: a new attempt needs a returned one.
    """
    validate_offset(offset)
    await require_user(session, actor_id=actor_id, role=Role.TEACHER)
    rows = (
        await session.execute(
            select(Submission, User.full_name, Assignment, StudyGroup.name)
            .join(User, User.id == Submission.student_id)
            .join(Assignment, Assignment.id == Submission.assignment_id)
            .join(StudyGroup, StudyGroup.id == Assignment.group_id)
            .where(
                StudyGroup.teacher_id == actor_id,
                Submission.status == SubmissionStatus.PENDING,
            )
            .order_by(Submission.submitted_at, Submission.id)
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        )
    ).all()
    return Page(
        items=tuple(PendingReview(*row) for row in rows[:PAGE_SIZE]),
        offset=offset,
        has_next=len(rows) > PAGE_SIZE,
    )
