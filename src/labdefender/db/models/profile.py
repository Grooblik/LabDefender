from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from labdefender.db.base import Base


class UserProfile(Base):
    """A name the user chose instead of the Telegram one. A missing row: the Telegram name.

    `User.full_name` always holds the name to show; while this row exists, refreshing the user
    from Telegram keeps it (see `services.users.ensure_user`).
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
