from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User
from labdefender.enums import Role


async def ensure_user(
    session: AsyncSession, *, telegram_id: int, username: str | None, full_name: str
) -> User:
    """Create or refresh Telegram identity, preserving the persisted role."""
    statement = insert(User).values(
        telegram_id=telegram_id, username=username, full_name=full_name, role=Role.STUDENT
    )
    statement = statement.on_conflict_do_update(
        index_elements=[User.telegram_id],
        set_={"username": statement.excluded.username, "full_name": statement.excluded.full_name},
    ).returning(User)
    try:
        user = (await session.scalars(statement)).one()
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return user
