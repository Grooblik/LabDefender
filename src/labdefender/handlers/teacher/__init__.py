from aiogram import Router

from labdefender.enums import Role
from labdefender.filters import RoleFilter


def create_router() -> Router:
    router = Router(name="teacher")
    router.message.filter(RoleFilter(Role.TEACHER))
    router.callback_query.filter(RoleFilter(Role.TEACHER))
    # Include teacher module routers here.
    return router
