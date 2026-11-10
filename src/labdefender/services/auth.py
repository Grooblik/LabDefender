import hmac
import math
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import TeacherRegistrationAttempt, User
from labdefender.db.transactions import immediate_transaction
from labdefender.enums import Role
from labdefender.services.access import require_user
from labdefender.services.errors import (
    AccessDenied,
    InvalidRegistrationCode,
    RegistrationBlocked,
    RegistrationDisabled,
    UserNotRegistered,
)

MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW = timedelta(minutes=10)


async def register_teacher(
    session: AsyncSession, *, actor_id: int, code: str, registration_code: SecretStr
) -> bool:
    """Return True on promotion, False if already teacher. Failed attempts are committed."""
    error = None
    async with immediate_transaction(session):
        user = await require_user(session, actor_id=actor_id)
        if user.role == Role.TEACHER:
            return False
        expected_code = registration_code.get_secret_value().strip()
        if not expected_code:
            raise RegistrationDisabled()

        now = datetime.now(UTC).replace(tzinfo=None)
        attempt = await session.get(TeacherRegistrationAttempt, actor_id)
        if attempt is not None and now >= attempt.window_started_at + ATTEMPT_WINDOW:
            attempt.failed_attempts = 0
            attempt.window_started_at = now

        if attempt is not None and attempt.failed_attempts >= MAX_FAILED_ATTEMPTS:
            error = RegistrationBlocked(
                max(
                    1, math.ceil((attempt.window_started_at + ATTEMPT_WINDOW - now).total_seconds())
                )
            )
        elif hmac.compare_digest(code.strip().encode("utf-8"), expected_code.encode("utf-8")):
            user.role = Role.TEACHER
            if attempt is not None:
                await session.delete(attempt)
        else:
            if attempt is None:
                attempt = TeacherRegistrationAttempt(
                    user_id=actor_id, failed_attempts=0, window_started_at=now
                )
                session.add(attempt)
            attempt.failed_attempts += 1
            if attempt.failed_attempts >= MAX_FAILED_ATTEMPTS:
                error = RegistrationBlocked(
                    max(
                        1,
                        math.ceil(
                            (attempt.window_started_at + ATTEMPT_WINDOW - now).total_seconds()
                        ),
                    )
                )
            else:
                error = InvalidRegistrationCode(MAX_FAILED_ATTEMPTS - attempt.failed_attempts)

    # Raise after commit so that a wrong code cannot roll back the failure counter.
    if error is not None:
        raise error
    return True


async def grant_teacher(
    session: AsyncSession, *, actor_id: int, target_telegram_id: int, admin_ids: frozenset[int]
) -> bool:
    """Admins are identified by the persisted Telegram ID, independently of their role."""
    async with immediate_transaction(session):
        actor = await require_user(session, actor_id=actor_id)
        if actor.telegram_id not in admin_ids:
            raise AccessDenied()
        target = await session.scalar(select(User).where(User.telegram_id == target_telegram_id))
        if target is None:
            raise UserNotRegistered()
        if target.role == Role.TEACHER:
            return False
        target.role = Role.TEACHER
        attempt = await session.get(TeacherRegistrationAttempt, target.id)
        if attempt is not None:
            await session.delete(attempt)
    return True
