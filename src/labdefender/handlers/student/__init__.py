from aiogram import Router

from labdefender.enums import Role
from labdefender.filters import RoleFilter


def create_router() -> Router:
    router = Router(name="student")
    router.message.filter(RoleFilter(Role.STUDENT))
    router.callback_query.filter(RoleFilter(Role.STUDENT))
    # Include student module routers here.
    return router
