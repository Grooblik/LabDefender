from datetime import UTC, date, datetime, time

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from labdefender.db.models import (
    Assignment,
    DefenseSlot,
    Membership,
    QueueEntry,
    StudyGroup,
    Submission,
    User,
)
from labdefender.enums import AssignmentFormat, Role, SubmissionKind, SubmissionStatus


@pytest.fixture
async def learning_context(database):
    _, sessions = database
    async with sessions() as session:
        teacher = User(telegram_id=1, full_name="Преподаватель", role=Role.TEACHER)
        student = User(telegram_id=2, full_name="Студент", role=Role.STUDENT)
        session.add_all([teacher, student])
        await session.flush()
        group = StudyGroup(teacher_id=teacher.id, name="ИВТ-101")
        session.add(group)
        await session.flush()
        session.add(Membership(group_id=group.id, student_id=student.id))
        assignment = Assignment(
            group_id=group.id,
            title="ЛР 1",
            description="Условие",
            format=AssignmentFormat.ONLINE,
        )
        session.add(assignment)
        await session.commit()
        return sessions, assignment.id, student.id


async def test_shared_schema_stores_solution_review_and_queue(learning_context):
    """Both role modules can read the same persisted submission and defense data."""
    sessions, assignment_id, student_id = learning_context
    approved_at = datetime.now(UTC).replace(tzinfo=None)
    async with sessions() as session:
        submission = Submission(
            assignment_id=assignment_id,
            student_id=student_id,
            attempt_number=1,
            kind=SubmissionKind.PDF,
            file_id="telegram-file-id",
            status=SubmissionStatus.APPROVED,
            approved_at=approved_at,
        )
        day = DefenseSlot(
            assignment_id=assignment_id,
            date=date(2026, 11, 13),
            start_time=time(14, 0),
            end_time=time(16, 0),
            capacity=1,
        )
        session.add_all([submission, day])
        await session.flush()
        session.add(QueueEntry(slot_id=day.id, submission_id=submission.id, order_at=approved_at))
        await session.commit()

    async with sessions() as session:
        entry, stored_submission, stored_day = (
            await session.execute(
                select(QueueEntry, Submission, DefenseSlot)
                .join(Submission, QueueEntry.submission_id == Submission.id)
                .join(DefenseSlot, QueueEntry.slot_id == DefenseSlot.id)
            )
        ).one()
        assert stored_submission.file_id == "telegram-file-id"
        assert stored_submission.status == SubmissionStatus.APPROVED
        assert entry.order_at == stored_submission.approved_at == approved_at
        assert stored_day.assignment_id == stored_submission.assignment_id
        group = await session.scalar(select(StudyGroup))
        assert len(group.invite_token) == 32


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"kind": SubmissionKind.PDF, "content": "not a file"},
        {"kind": SubmissionKind.TEXT, "content": "text", "file_id": "file"},
        {"kind": SubmissionKind.TEXT, "content": "text", "status": SubmissionStatus.RETURNED},
        {"kind": SubmissionKind.TEXT, "content": "text", "status": SubmissionStatus.APPROVED},
    ],
)
async def test_database_rejects_incomplete_solutions(learning_context, invalid_payload):
    sessions, assignment_id, student_id = learning_context
    async with sessions() as session:
        session.add(
            Submission(
                assignment_id=assignment_id,
                student_id=student_id,
                attempt_number=1,
                **invalid_payload,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_same_attempt_cannot_be_saved_twice(learning_context):
    sessions, assignment_id, student_id = learning_context
    async with sessions() as session:
        for _ in range(2):
            session.add(
                Submission(
                    assignment_id=assignment_id,
                    student_id=student_id,
                    attempt_number=1,
                    kind=SubmissionKind.TEXT,
                    content="Решение",
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
