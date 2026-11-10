from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from labdefender.db.base import Base
from labdefender.enums import Role


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(32))
    full_name: Mapped[str] = mapped_column(String(256))
    role: Mapped[Role] = mapped_column(
        Enum(
            Role,
            values_callable=lambda roles: [role.value for role in roles],
            native_enum=False,
            create_constraint=True,
            name="user_role",
        ),
        default=Role.STUDENT,
    )
    # SQLite stores UTC without a timezone offset. All application timestamps use UTC.
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
