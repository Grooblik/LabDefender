from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from labdefender.db.base import Base


class NotificationSetting(Base):
    """Teacher notification switches. A missing row means every category is enabled."""

    __tablename__ = "notification_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    submissions: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    joins: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    queue: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
