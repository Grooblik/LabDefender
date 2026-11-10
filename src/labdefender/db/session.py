from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from labdefender.db import models  # noqa: F401 -- register all models in Base.metadata
from labdefender.db.base import Base


def create_database(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    database = make_url(url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(url, connect_args={"timeout": 10})

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(engine: AsyncEngine) -> None:
    # Creates missing tables only; this is not a schema migration mechanism.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
