from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import Assignment
from labdefender.db.transactions import immediate_transaction
from labdefender.enums import AssignmentFormat
from labdefender.services.access import require_assignment_access, require_group_owner
from labdefender.services.errors import InvalidAssignment, InvalidAssignmentFormat
from labdefender.services.groups import get_group
from labdefender.services.pagination import PAGE_SIZE, Page, validate_offset


def text_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def validate_assignment(title: str, description: str) -> tuple[str, str]:
    title, description = " ".join(title.split()), description.strip()
    if not 1 <= len(title) <= 256 or not 1 <= text_units(description) <= 4000:
        raise InvalidAssignment()
    return title, description


def normalize_assignment_format(value: AssignmentFormat | str) -> AssignmentFormat:
    try:
        return value if isinstance(value, AssignmentFormat) else AssignmentFormat(value)
    except ValueError:
        raise InvalidAssignmentFormat() from None


async def create_assignment(
    session: AsyncSession,
    *,
    actor_id: int,
    group_id: int,
    title: str,
    description: str,
    format: AssignmentFormat | str,
) -> Assignment:
    title, description = validate_assignment(title, description)
    assignment_format = normalize_assignment_format(format)
    async with immediate_transaction(session):
        await require_group_owner(session, actor_id=actor_id, group_id=group_id)
        assignment = Assignment(
            group_id=group_id,
            title=title,
            description=description,
            format=assignment_format,
        )
        session.add(assignment)
        await session.flush()
    return assignment


async def list_assignments(
    session: AsyncSession, *, actor_id: int, group_id: int, offset: int = 0
) -> Page[Assignment]:
    validate_offset(offset)
    await get_group(session, actor_id=actor_id, group_id=group_id)
    items = tuple(
        (
            await session.scalars(
                select(Assignment)
                .where(Assignment.group_id == group_id)
                .order_by(Assignment.id)
                .offset(offset)
                .limit(PAGE_SIZE + 1)
            )
        ).all()
    )
    return Page(items=items[:PAGE_SIZE], offset=offset, has_next=len(items) > PAGE_SIZE)


async def get_assignment(session: AsyncSession, *, actor_id: int, assignment_id: int) -> Assignment:
    return await require_assignment_access(session, actor_id=actor_id, assignment_id=assignment_id)
