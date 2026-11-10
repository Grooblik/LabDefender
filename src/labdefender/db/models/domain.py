import secrets
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from labdefender.db.base import Base
from labdefender.enums import SubmissionKind, SubmissionStatus


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


class DefenseDay(Base):
    __tablename__ = "defense_days"
    __table_args__ = (
        UniqueConstraint("assignment_id", "date"),
        CheckConstraint("capacity > 0", name="positive_capacity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    capacity: Mapped[int]


class QueueEntry(Base):
    __tablename__ = "queue_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    defense_day_id: Mapped[int] = mapped_column(ForeignKey("defense_days.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), unique=True)
    # Initial value = submission.approved_at; moving to the end sets this to now (UTC).
    order_at: Mapped[datetime] = mapped_column(DateTime)
