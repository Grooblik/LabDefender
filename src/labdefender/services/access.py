from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import Assignment, DefenseDay, Membership, StudyGroup, Submission, User
from labdefender.enums import Role
from labdefender.services.errors import AccessDenied, ObjectUnavailable


async def require_user(session: AsyncSession, *, actor_id: int, role: Role | None = None) -> User:
    user = await session.scalar(
        select(User).where(User.id == actor_id).execution_options(populate_existing=True)
    )
    if user is None or (role is not None and user.role != role):
        raise AccessDenied()
    return user


async def require_group_owner(session: AsyncSession, *, actor_id: int, group_id: int) -> StudyGroup:
    await require_user(session, actor_id=actor_id, role=Role.TEACHER)
    group = await session.scalar(
        select(StudyGroup).where(StudyGroup.id == group_id, StudyGroup.teacher_id == actor_id)
    )
    if group is None:
        raise ObjectUnavailable()
    return group


async def require_group_member(
    session: AsyncSession, *, actor_id: int, group_id: int
) -> StudyGroup:
    await require_user(session, actor_id=actor_id, role=Role.STUDENT)
    group = await session.scalar(
        select(StudyGroup)
        .join(Membership, Membership.group_id == StudyGroup.id)
        .where(StudyGroup.id == group_id, Membership.student_id == actor_id)
    )
    if group is None:
        raise ObjectUnavailable()
    return group


async def require_assignment_access(
    session: AsyncSession, *, actor_id: int, assignment_id: int
) -> Assignment:
    user = await require_user(session, actor_id=actor_id)
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        raise ObjectUnavailable()
    if user.role == Role.TEACHER:
        await require_group_owner(session, actor_id=actor_id, group_id=assignment.group_id)
    else:
        await require_group_member(session, actor_id=actor_id, group_id=assignment.group_id)
    return assignment


async def require_submission_access(
    session: AsyncSession, *, actor_id: int, submission_id: int
) -> Submission:
    user = await require_user(session, actor_id=actor_id)
    submission = await session.get(Submission, submission_id)
    if submission is None or (user.role == Role.STUDENT and submission.student_id != actor_id):
        raise ObjectUnavailable()
    await require_assignment_access(
        session, actor_id=actor_id, assignment_id=submission.assignment_id
    )
    return submission


async def require_defense_day_access(
    session: AsyncSession, *, actor_id: int, defense_day_id: int
) -> DefenseDay:
    day = await session.get(DefenseDay, defense_day_id)
    if day is None:
        raise ObjectUnavailable()
    await require_assignment_access(session, actor_id=actor_id, assignment_id=day.assignment_id)
    return day
