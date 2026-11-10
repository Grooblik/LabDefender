from aiogram import Router

from labdefender.enums import Role
from labdefender.filters import RoleFilter
from labdefender.handlers.teacher import coursework, defense, groups


def create_router() -> Router:
    router = Router(name="teacher")
    router.message.filter(RoleFilter(Role.TEACHER))
    router.callback_query.filter(RoleFilter(Role.TEACHER))
    router.include_routers(
        groups.create_router(), coursework.create_router(), defense.create_router()
    )
    return router
