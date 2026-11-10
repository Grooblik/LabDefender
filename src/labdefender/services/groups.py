import re
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import Membership, StudyGroup
from labdefender.db.transactions import immediate_transaction
from labdefender.enums import Role
from labdefender.services.access import require_group_member, require_group_owner, require_user
from labdefender.services.errors import (
    GroupCreationUnavailable,
    InvalidGroupName,
    InvalidInvitation,
    InvitationUnavailable,
    ObjectUnavailable,
)

GROUP_PAGE_SIZE = 8


@dataclass(frozen=True)
class GroupPage:
    groups: tuple[StudyGroup, ...]
    offset: int
    has_next: bool


@dataclass(frozen=True)
class JoinGroupResult:
    group: StudyGroup
    joined: bool


def normalize_group_name(name: str) -> str:
    name = " ".join(name.split())
    if not 1 <= len(name) <= 128:
        raise InvalidGroupName()
    return name


def parse_invite_payload(payload: str) -> str:
    match = re.fullmatch(r"join_([A-Za-z0-9_-]{32})", payload)
    if match is None:
        raise InvalidInvitation()
    return match[1]


async def create_group(session: AsyncSession, *, actor_id: int, name: str) -> StudyGroup:
    name = normalize_group_name(name)
    async with immediate_transaction(session):
        await require_user(session, actor_id=actor_id, role=Role.TEACHER)
        # The write lock prevents a second writer from claiming the same token after this check.
        for _ in range(5):
            token = secrets.token_urlsafe(24)
            existing = await session.scalar(
                select(StudyGroup.id).where(StudyGroup.invite_token == token)
            )
            if existing is None:
                break
        else:
            raise GroupCreationUnavailable()
        group = StudyGroup(teacher_id=actor_id, name=name, invite_token=token)
        session.add(group)
        await session.flush()
    return group


async def list_groups(session: AsyncSession, *, actor_id: int, offset: int = 0) -> GroupPage:
    if not 0 <= offset <= 2**31 - 1:
        raise ObjectUnavailable()
    user = await require_user(session, actor_id=actor_id)
    query = select(StudyGroup)
    if user.role == Role.TEACHER:
        query = query.where(StudyGroup.teacher_id == actor_id)
    else:
        query = query.join(Membership, Membership.group_id == StudyGroup.id).where(
            Membership.student_id == actor_id
        )
    groups = tuple(
        (
            await session.scalars(
                query.order_by(StudyGroup.id).offset(offset).limit(GROUP_PAGE_SIZE + 1)
            )
        ).all()
    )
    return GroupPage(
        groups=groups[:GROUP_PAGE_SIZE], offset=offset, has_next=len(groups) > GROUP_PAGE_SIZE
    )


async def get_group(session: AsyncSession, *, actor_id: int, group_id: int) -> StudyGroup:
    user = await require_user(session, actor_id=actor_id)
    if user.role == Role.TEACHER:
        return await require_group_owner(session, actor_id=actor_id, group_id=group_id)
    return await require_group_member(session, actor_id=actor_id, group_id=group_id)


async def get_invite_link(
    session: AsyncSession, *, actor_id: int, group_id: int, bot_username: str
) -> str:
    group = await require_group_owner(session, actor_id=actor_id, group_id=group_id)
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username) is None:
        raise InvitationUnavailable()
    return f"https://t.me/{bot_username}?start=join_{group.invite_token}"


async def join_group(session: AsyncSession, *, actor_id: int, invite_token: str) -> JoinGroupResult:
    if re.fullmatch(r"[A-Za-z0-9_-]{32}", invite_token) is None:
        raise InvalidInvitation()
    async with immediate_transaction(session):
        await require_user(session, actor_id=actor_id, role=Role.STUDENT)
        group = await session.scalar(
            select(StudyGroup).where(StudyGroup.invite_token == invite_token)
        )
        if group is None:
            raise InvalidInvitation()
        joined = await session.get(Membership, (group.id, actor_id)) is None
        if joined:
            session.add(Membership(group_id=group.id, student_id=actor_id))
    return JoinGroupResult(group=group, joined=joined)
