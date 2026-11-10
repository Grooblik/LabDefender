from datetime import date

import pytest

from labdefender.db.models import (
    Assignment,
    DefenseDay,
    Membership,
    StudyGroup,
    Submission,
    User,
)
from labdefender.enums import Role, SubmissionKind
from labdefender.services.access import (
    require_assignment_access,
    require_defense_day_access,
    require_group_member,
    require_group_owner,
    require_submission_access,
)
from labdefender.services.errors import DomainError


@pytest.fixture
async def context(database):
    _, sessions = database
    async with sessions() as session:
        users = [
            User(telegram_id=1, full_name="Teacher", role=Role.TEACHER),
            User(telegram_id=2, full_name="Other teacher", role=Role.TEACHER),
            User(telegram_id=3, full_name="Student", role=Role.STUDENT),
            User(telegram_id=4, full_name="Classmate", role=Role.STUDENT),
            User(telegram_id=5, full_name="Outsider", role=Role.STUDENT),
        ]
        session.add_all(users)
        await session.flush()
        group = StudyGroup(teacher_id=users[0].id, name="Group")
        session.add(group)
        await session.flush()
        session.add_all(
            [
                Membership(group_id=group.id, student_id=users[2].id),
                Membership(group_id=group.id, student_id=users[3].id),
            ]
        )
        assignment = Assignment(group_id=group.id, title="Lab", description="Task")
        session.add(assignment)
        await session.flush()
        submission = Submission(
            assignment_id=assignment.id,
            student_id=users[2].id,
            attempt_number=1,
            kind=SubmissionKind.TEXT,
            content="Private solution",
        )
        day = DefenseDay(assignment_id=assignment.id, date=date(2026, 11, 13), capacity=1)
        session.add_all([submission, day])
        await session.commit()
        return sessions, {
            "teacher": users[0].id,
            "other_teacher": users[1].id,
            "student": users[2].id,
            "classmate": users[3].id,
            "outsider": users[4].id,
            "group": group.id,
            "assignment": assignment.id,
            "submission": submission.id,
            "day": day.id,
        }


@pytest.mark.parametrize("actor", ["teacher", "other_teacher", "student", "outsider"])
async def test_group_ownership_checks_persisted_role_and_owner(context, actor):
    sessions, ids = context
    async with sessions() as session:
        if actor == "teacher":
            assert (
                await require_group_owner(session, actor_id=ids[actor], group_id=ids["group"])
            ).id == ids["group"]
        else:
            with pytest.raises(DomainError):
                await require_group_owner(session, actor_id=ids[actor], group_id=ids["group"])


@pytest.mark.parametrize("actor", ["student", "classmate", "outsider", "teacher"])
async def test_membership_is_required_for_student_access(context, actor):
    sessions, ids = context
    async with sessions() as session:
        if actor in {"student", "classmate"}:
            assert (
                await require_group_member(session, actor_id=ids[actor], group_id=ids["group"])
            ).id == ids["group"]
        else:
            with pytest.raises(DomainError):
                await require_group_member(session, actor_id=ids[actor], group_id=ids["group"])


@pytest.mark.parametrize("actor", ["teacher", "student", "other_teacher", "outsider"])
async def test_assignment_and_defense_day_follow_group_access(context, actor):
    sessions, ids = context
    async with sessions() as session:
        if actor in {"teacher", "student"}:
            assert (
                await require_assignment_access(
                    session, actor_id=ids[actor], assignment_id=ids["assignment"]
                )
            ).id == ids["assignment"]
            assert (
                await require_defense_day_access(
                    session, actor_id=ids[actor], defense_day_id=ids["day"]
                )
            ).id == ids["day"]
        else:
            with pytest.raises(DomainError):
                await require_assignment_access(
                    session, actor_id=ids[actor], assignment_id=ids["assignment"]
                )
            with pytest.raises(DomainError):
                await require_defense_day_access(
                    session, actor_id=ids[actor], defense_day_id=ids["day"]
                )


@pytest.mark.parametrize("actor", ["teacher", "student", "classmate", "other_teacher", "outsider"])
async def test_submission_is_private_even_from_classmates(context, actor):
    sessions, ids = context
    async with sessions() as session:
        if actor in {"teacher", "student"}:
            assert (
                await require_submission_access(
                    session, actor_id=ids[actor], submission_id=ids["submission"]
                )
            ).content == "Private solution"
        else:
            with pytest.raises(DomainError):
                await require_submission_access(
                    session, actor_id=ids[actor], submission_id=ids["submission"]
                )


async def test_missing_objects_and_unknown_users_fail_closed(context):
    sessions, ids = context
    async with sessions() as session:
        with pytest.raises(DomainError):
            await require_group_owner(session, actor_id=404, group_id=ids["group"])
        with pytest.raises(DomainError):
            await require_submission_access(session, actor_id=ids["student"], submission_id=404)
