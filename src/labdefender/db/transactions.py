from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def immediate_transaction(session: AsyncSession) -> AsyncIterator[None]:
    """Serialize SQLite check-and-write operations. Call before any session reads."""
    async with session.begin():
        await session.execute(text("BEGIN IMMEDIATE"))
        yield
