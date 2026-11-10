from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from labdefender.db.base import Base


class TeacherRegistrationAttempt(Base):
    __tablename__ = "teacher_registration_attempts"
    __table_args__ = (
        CheckConstraint("failed_attempts >= 0 AND failed_attempts <= 5", name="attempt_limit"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    failed_attempts: Mapped[int]
    window_started_at: Mapped[datetime] = mapped_column(DateTime)
