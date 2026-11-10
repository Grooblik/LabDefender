from aiogram import Router

from labdefender.enums import Role
from labdefender.filters import RoleFilter
from labdefender.handlers.student import groups


def create_router() -> Router:
    router = Router(name="student")
    router.message.filter(RoleFilter(Role.STUDENT))
    router.callback_query.filter(RoleFilter(Role.STUDENT))
    router.include_router(groups.create_router())
    return router
