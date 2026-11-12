"""Progress reports: a discipline (group) table and a student's report across disciplines.

Read-only. Rights: a discipline report — only its teacher. A student's report — the student
(all their groups) or a teacher (only the teacher's own groups the student is in); other
teachers' disciplines are never shown to a teacher.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import (
    Assignment,
    DefenseSlot,
    Membership,
    QueueEntry,
    StudyGroup,
    Submission,
    User,
)
from labdefender.enums import Role, SubmissionStatus
from labdefender.services.access import require_group_owner, require_user
from labdefender.services.errors import AccessDenied, ObjectUnavailable
from labdefender.services.queue import MOSCOW
from labdefender.services.submissions import STATUS_LABELS

NOT_SUBMITTED = "Не сдано"
STATUS_MARKS = {
    None: "—",
    SubmissionStatus.PENDING: "⏳",
    SubmissionStatus.RETURNED: "🔄",
    SubmissionStatus.APPROVED: "✅",
}


def moscow(moment: datetime | None, pattern: str = "%d.%m.%Y %H:%M") -> str:
    """Stored timestamps are naive UTC."""
    if moment is None:
        return ""
    return moment.replace(tzinfo=UTC).astimezone(MOSCOW).strftime(pattern)


@dataclass(frozen=True)
class WorkCell:
    """What one student did on one assignment: the latest attempt and the defense booking."""

    status: SubmissionStatus | None = None
    attempts: int = 0
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    slot: DefenseSlot | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status] if self.status else NOT_SUBMITTED

    @property
    def defense_label(self) -> str:
        if self.slot is None:
            return ""
        return f"{self.slot.date:%d.%m.%Y} {self.slot.start_time:%H:%M}–{self.slot.end_time:%H:%M}"

    @property
    def short(self) -> str:
        """Compact table cell: mark, then the most telling date or the attempts."""
        mark = STATUS_MARKS[self.status]
        if self.status == SubmissionStatus.APPROVED:
            text = f"{mark} {moscow(self.approved_at, '%d.%m')}"
        elif self.status == SubmissionStatus.RETURNED:
            text = f"{mark} ×{self.attempts}"
        elif self.status == SubmissionStatus.PENDING:
            text = f"{mark} {moscow(self.submitted_at, '%d.%m')}"
        else:
            text = mark
        return text + (f" · 🗓 {self.slot.date:%d.%m}" if self.slot else "")

    @property
    def full(self) -> str:
        """One line for a file or a list: status, attempts, dates, defense."""
        if self.status is None:
            return NOT_SUBMITTED
        parts = [self.status_label, f"попыток: {self.attempts}"]
        parts.append(f"последняя {moscow(self.submitted_at)}")
        if self.approved_at:
            parts.append(f"допущено {moscow(self.approved_at)}")
        if self.slot:
            parts.append(f"защита {self.defense_label}")
        return ", ".join(parts)


@dataclass(frozen=True)
class GroupReport:
    group: StudyGroup
    assignments: tuple[Assignment, ...]
    students: tuple[User, ...]
    cells: dict[tuple[int, int], WorkCell]

    def cell(self, student_id: int, assignment_id: int) -> WorkCell:
        return self.cells.get((student_id, assignment_id), WorkCell())

    def approved_count(self, student_id: int) -> int:
        return sum(
            self.cell(student_id, assignment.id).status == SubmissionStatus.APPROVED
            for assignment in self.assignments
        )


async def _cells(
    session: AsyncSession, *, group_ids: list[int], student_ids: list[int]
) -> dict[tuple[int, int], WorkCell]:
    if not group_ids or not student_ids:
        return {}
    submissions = (
        await session.scalars(
            select(Submission)
            .join(Assignment, Assignment.id == Submission.assignment_id)
            .where(Assignment.group_id.in_(group_ids), Submission.student_id.in_(student_ids))
            .order_by(Submission.attempt_number)
        )
    ).all()
    latest: dict[tuple[int, int], Submission] = {}
    attempts: dict[tuple[int, int], int] = defaultdict(int)
    for submission in submissions:
        key = (submission.student_id, submission.assignment_id)
        latest[key] = submission
        attempts[key] += 1
    slots = {
        (entry.student_id, entry.assignment_id): slot
        for entry, slot in (
            await session.execute(
                select(QueueEntry, DefenseSlot)
                .join(DefenseSlot, DefenseSlot.id == QueueEntry.slot_id)
                .where(DefenseSlot.group_id.in_(group_ids), QueueEntry.student_id.in_(student_ids))
            )
        ).all()
    }
    return {
        key: WorkCell(
            status=submission.status,
            attempts=attempts[key],
            submitted_at=submission.submitted_at,
            approved_at=submission.approved_at,
            slot=slots.get(key),
        )
        for key, submission in latest.items()
    }


async def _assignments(session: AsyncSession, group_ids: list[int]) -> tuple[Assignment, ...]:
    return tuple(
        await session.scalars(
            select(Assignment)
            .where(Assignment.group_id.in_(group_ids))
            .order_by(Assignment.group_id, Assignment.id)
        )
    )


async def group_report(session: AsyncSession, *, actor_id: int, group_id: int) -> GroupReport:
    """Students of the teacher's group × its assignments, students by name."""
    group = await require_group_owner(session, actor_id=actor_id, group_id=group_id)
    students = tuple(
        await session.scalars(
            select(User)
            .join(Membership, Membership.student_id == User.id)
            .where(Membership.group_id == group_id)
            .order_by(User.full_name, User.id)
        )
    )
    return GroupReport(
        group=group,
        assignments=await _assignments(session, [group_id]),
        students=students,
        cells=await _cells(
            session, group_ids=[group_id], student_ids=[student.id for student in students]
        ),
    )


@dataclass(frozen=True)
class StudentDiscipline:
    group: StudyGroup
    rows: tuple[tuple[Assignment, WorkCell], ...]


@dataclass(frozen=True)
class StudentReport:
    student: User
    disciplines: tuple[StudentDiscipline, ...]


async def student_report(
    session: AsyncSession, *, actor_id: int, student_id: int | None = None
) -> StudentReport:
    """A student's own report (every group), or a teacher's view of it (own groups only)."""
    actor = await require_user(session, actor_id=actor_id)
    if actor.role == Role.STUDENT:
        if student_id not in (None, actor_id):
            raise AccessDenied()
        student = actor
        groups_query = (
            select(StudyGroup)
            .join(Membership, Membership.group_id == StudyGroup.id)
            .where(Membership.student_id == actor_id)
        )
    else:
        student = await session.get(User, student_id) if student_id else None
        if student is None:
            raise ObjectUnavailable()
        groups_query = (
            select(StudyGroup)
            .join(Membership, Membership.group_id == StudyGroup.id)
            .where(Membership.student_id == student.id, StudyGroup.teacher_id == actor_id)
        )
    groups = tuple(await session.scalars(groups_query.order_by(StudyGroup.name, StudyGroup.id)))
    if actor.role == Role.TEACHER and not groups:
        # Not a student of this teacher: the teacher learns nothing about them.
        raise ObjectUnavailable()
    group_ids = [group.id for group in groups]
    assignments = await _assignments(session, group_ids)
    cells = await _cells(session, group_ids=group_ids, student_ids=[student.id])
    disciplines = tuple(
        StudentDiscipline(
            group=group,
            rows=tuple(
                (assignment, cells.get((student.id, assignment.id), WorkCell()))
                for assignment in assignments
                if assignment.group_id == group.id
            ),
        )
        for group in groups
    )
    return StudentReport(student=student, disciplines=disciplines)
