import secrets
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from labdefender.db.base import Base
from labdefender.enums import AssignmentFormat, SubmissionKind, SubmissionStatus


class StudyGroup(Base):
    __tablename__ = "study_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    invite_token: Mapped[str] = mapped_column(
        String(32), unique=True, default=lambda: secrets.token_urlsafe(24)
    )


class Membership(Base):
    __tablename__ = "memberships"

    group_id: Mapped[int] = mapped_column(ForeignKey("study_groups.id"), primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("study_groups.id"), index=True)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text)
    format: Mapped[AssignmentFormat] = mapped_column(
        Enum(
            AssignmentFormat,
            values_callable=lambda formats: [item.value for item in formats],
            native_enum=False,
            create_constraint=True,
            name="assignment_format",
        )
    )


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("assignment_id", "student_id", "attempt_number"),
        CheckConstraint("attempt_number > 0", name="positive_attempt"),
        CheckConstraint(
            "(kind IN ('pdf', 'zip') AND file_id IS NOT NULL AND content IS NULL) OR "
            "(kind IN ('text', 'link') AND content IS NOT NULL AND file_id IS NULL)",
            name="payload_matches_kind",
        ),
        CheckConstraint(
            "status != 'returned' OR (review_comment IS NOT NULL AND length(review_comment) > 0)",
            name="return_requires_comment",
        ),
        CheckConstraint(
            "status != 'approved' OR approved_at IS NOT NULL", name="approval_requires_time"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    attempt_number: Mapped[int]
    kind: Mapped[SubmissionKind] = mapped_column(
        Enum(
            SubmissionKind,
            values_callable=lambda kinds: [kind.value for kind in kinds],
            native_enum=False,
            create_constraint=True,
            name="submission_kind",
        )
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(
            SubmissionStatus,
            values_callable=lambda statuses: [status.value for status in statuses],
            native_enum=False,
            create_constraint=True,
            name="submission_status",
        ),
        default=SubmissionStatus.PENDING,
    )
    content: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(Text)
    review_comment: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class DefenseSlot(Base):
    """A defense session of a group (discipline): any approved work of the group can be booked.

    Slots of one group must not overlap; the services check it. There is no unique constraint:
    databases migrated from per-assignment slots may keep two slots at the same time.
    """

    __tablename__ = "defense_slots"
    __table_args__ = (
        CheckConstraint("start_time < end_time", name="valid_time_range"),
        CheckConstraint("capacity > 0", name="positive_capacity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("study_groups.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    capacity: Mapped[int]
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")


class QueueEntry(Base):
    """One booked work: the student, the assignment and the approved attempt (submission).

    `student_id` and `assignment_id` repeat the submission's values, so the database itself
    keeps one booking per student and assignment; a student may book several works.
    """

    __tablename__ = "queue_entries"
    __table_args__ = (UniqueConstraint("student_id", "assignment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("defense_slots.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), unique=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    # Initial value = submission.approved_at; moving to the end sets this to now (UTC).
    order_at: Mapped[datetime] = mapped_column(DateTime)
