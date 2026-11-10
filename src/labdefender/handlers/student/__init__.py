from aiogram import Router

from labdefender.enums import Role
from labdefender.filters import RoleFilter
from labdefender.handlers.student import coursework, groups


def create_router() -> Router:
    router = Router(name="student")
    router.message.filter(RoleFilter(Role.STUDENT))
    router.callback_query.filter(RoleFilter(Role.STUDENT))
    router.include_routers(groups.create_router(), coursework.create_router())
    return router
