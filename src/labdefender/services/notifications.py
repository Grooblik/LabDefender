"""Notification preferences and recipient lookups. Independent of Telegram.

Preference changes check the actor's role. Lookup functions do not check rights: the notifier
calls them only after a service has authorized and committed the action, with IDs of the
objects that action changed. They return ORM objects for building messages, never send them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import (
    Assignment,
    DefenseSlot,
    Membership,
    NotificationSetting,
    QueueEntry,
    StudyGroup,
    Submission,
    User,
)
from labdefender.db.transactions import immediate_transaction
from labdefender.enums import Role
from labdefender.services.access import require_user
from labdefender.services.queue import approved_unassigned_query


class NoticeCategory(StrEnum):
    """Teacher notification categories; students always receive their notifications."""

    SUBMISSIONS = "submissions"
    JOINS = "joins"
    QUEUE = "queue"


@dataclass(frozen=True)
class NotificationPreferences:
    submissions: bool = True
    joins: bool = True
    queue: bool = True

    def enabled(self, category: NoticeCategory) -> bool:
        return getattr(self, category.value)


def _preferences(row: NotificationSetting | None) -> NotificationPreferences:
    if row is None:
        return NotificationPreferences()
    return NotificationPreferences(submissions=row.submissions, joins=row.joins, queue=row.queue)


async def teacher_preferences(session: AsyncSession, *, teacher_id: int) -> NotificationPreferences:
    return _preferences(await session.get(NotificationSetting, teacher_id))


async def get_notification_preferences(
    session: AsyncSession, *, actor_id: int
) -> NotificationPreferences:
    await require_user(session, actor_id=actor_id, role=Role.TEACHER)
    return await teacher_preferences(session, teacher_id=actor_id)


async def set_notification_preferences(
    session: AsyncSession,
    *,
    actor_id: int,
    categories: Iterable[NoticeCategory],
    enabled: bool,
) -> NotificationPreferences:
    """Switch the given categories of the acting teacher on or off. Idempotent."""
    categories = [NoticeCategory(category) for category in categories]
    async with immediate_transaction(session):
        await require_user(session, actor_id=actor_id, role=Role.TEACHER)
        row = await session.get(NotificationSetting, actor_id)
        if row is None:
            row = NotificationSetting(user_id=actor_id, submissions=True, joins=True, queue=True)
            session.add(row)
        for category in categories:
            setattr(row, category.value, bool(enabled))
        await session.flush()
        preferences = _preferences(row)
    return preferences


@dataclass(frozen=True)
class SubmissionContext:
    submission: Submission
    assignment: Assignment
    group: StudyGroup
    student: User
    teacher: User


async def submission_context(
    session: AsyncSession, *, submission_id: int
) -> SubmissionContext | None:
    row = (
        await session.execute(
            select(Submission, Assignment, StudyGroup)
            .join(Assignment, Assignment.id == Submission.assignment_id)
            .join(StudyGroup, StudyGroup.id == Assignment.group_id)
            .where(Submission.id == submission_id)
        )
    ).one_or_none()
    if row is None:
        return None
    submission, assignment, group = row
    student = await session.get(User, submission.student_id)
    teacher = await session.get(User, group.teacher_id)
    return SubmissionContext(submission, assignment, group, student, teacher)


async def group_students(session: AsyncSession, *, group_id: int) -> tuple[User, ...]:
    users = await session.scalars(
        select(User)
        .join(Membership, Membership.student_id == User.id)
        .where(Membership.group_id == group_id, User.role == Role.STUDENT)
        .order_by(User.id)
    )
    return tuple(users)


async def approved_unbooked_students(session: AsyncSession, *, group_id: int) -> tuple[User, ...]:
    """Students of the group with at least one approved work not booked into a slot."""
    users = await session.scalars(
        approved_unassigned_query(group_id, User)
        .where(User.role == Role.STUDENT)
        .group_by(User.id)
        .order_by(User.id)
    )
    return tuple(users)


@dataclass(frozen=True)
class SlotContext:
    slot: DefenseSlot
    group: StudyGroup
    occupied: int


async def slot_context(session: AsyncSession, *, slot_id: int) -> SlotContext | None:
    row = (
        await session.execute(
            select(DefenseSlot, StudyGroup)
            .join(StudyGroup, StudyGroup.id == DefenseSlot.group_id)
            .where(DefenseSlot.id == slot_id)
        )
    ).one_or_none()
    if row is None:
        return None
    occupied = await session.scalar(
        select(func.count()).select_from(QueueEntry).where(QueueEntry.slot_id == slot_id)
    )
    return SlotContext(*row, occupied=occupied)
