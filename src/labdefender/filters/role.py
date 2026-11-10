from aiogram.filters import Filter
from aiogram.types import TelegramObject

from labdefender.db.models import User
from labdefender.enums import Role


class RoleFilter(Filter):
    def __init__(self, role: Role) -> None:
        self.role = role

    async def __call__(self, event: TelegramObject, current_user: User | None = None) -> bool:
        return current_user is not None and current_user.role == self.role
