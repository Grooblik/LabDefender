from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import User, UserProfile
from labdefender.db.transactions import immediate_transaction
from labdefender.enums import Role
from labdefender.services.access import require_user
from labdefender.services.errors import InvalidDisplayName

DISPLAY_NAME_LIMIT = 128


async def ensure_user(
    session: AsyncSession, *, telegram_id: int, username: str | None, full_name: str
) -> User:
    """Create or refresh Telegram identity, preserving the persisted role and a chosen name."""
    statement = insert(User).values(
        telegram_id=telegram_id, username=username, full_name=full_name, role=Role.STUDENT
    )
    # `users.id` is the existing row of the upsert; SQLAlchemy cannot correlate an INSERT.
    chosen = (
        select(UserProfile.display_name)
        .where(UserProfile.user_id == literal_column("users.id"))
        .scalar_subquery()
    )
    statement = statement.on_conflict_do_update(
        index_elements=[User.telegram_id],
        set_={
            "username": statement.excluded.username,
            "full_name": func.coalesce(chosen, statement.excluded.full_name),
        },
    ).returning(User)
    try:
        user = (await session.scalars(statement)).one()
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return user


def normalize_display_name(value: str) -> str:
    """Spaces and line breaks collapse into single spaces, as for group names."""
    name = " ".join(value.split())
    if not 1 <= len(name) <= DISPLAY_NAME_LIMIT:
        raise InvalidDisplayName()
    return name


async def has_chosen_name(session: AsyncSession, *, user_id: int) -> bool:
    return await session.get(UserProfile, user_id) is not None


async def set_display_name(session: AsyncSession, *, actor_id: int, name: str) -> User:
    """Show the user under `name` everywhere: lists, queues, notifications. Idempotent."""
    name = normalize_display_name(name)
    async with immediate_transaction(session):
        user = await require_user(session, actor_id=actor_id)
        profile = await session.get(UserProfile, actor_id)
        if profile is None:
            session.add(UserProfile(user_id=actor_id, display_name=name))
        else:
            profile.display_name = name
        user.full_name = name
    return user


async def reset_display_name(session: AsyncSession, *, actor_id: int, telegram_name: str) -> User:
    """Return to the name from Telegram; later updates refresh it from Telegram again."""
    async with immediate_transaction(session):
        user = await require_user(session, actor_id=actor_id)
        profile = await session.get(UserProfile, actor_id)
        if profile is not None:
            await session.delete(profile)
        user.full_name = telegram_name[:256] or str(user.telegram_id)
    return user
