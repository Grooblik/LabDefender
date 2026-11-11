"""Profile: who is signed in, the chosen name, the teacher code and the role grant."""

import logging

from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.handlers.registration import TEACHER_MENU_HINT, parse_telegram_id
from labdefender.keyboards.menu import main_menu
from labdefender.services.auth import grant_teacher, register_teacher
from labdefender.services.errors import InvalidDisplayName, InvalidRegistrationCode
from labdefender.services.users import (
    DISPLAY_NAME_LIMIT,
    has_chosen_name,
    reset_display_name,
    set_display_name,
)
from labdefender.web.context import (
    BOT,
    CONFIG,
    TELEGRAM_NAME,
    InvalidRequest,
    chat_state,
    current_user,
    db,
    read_body,
)

logger = logging.getLogger(__name__)

ROLE_LABELS = {Role.STUDENT: "студент", Role.TEACHER: "преподаватель"}
CODE_LIMIT = 4096

routes = web.RouteTableDef()


class TeacherCode(BaseModel):
    model_config = ConfigDict(strict=True)
    code: str = Field(max_length=CODE_LIMIT)


class DisplayNameDraft(BaseModel):
    model_config = ConfigDict(strict=True)
    # Longer input may still shrink to the limit once spaces are collapsed.
    name: str = Field(max_length=DISPLAY_NAME_LIMIT * 4)


class TeacherGrant(BaseModel):
    model_config = ConfigDict(strict=True)
    # A string: Telegram IDs may exceed the integers JavaScript represents exactly.
    telegram_id: str = Field(max_length=32)


async def profile_json(request: web.Request, user: User) -> dict:
    config = request.app[CONFIG]
    return {
        "telegram_id": user.telegram_id,
        "name": user.full_name,
        # The name from Telegram and whether the user replaced it with their own.
        "telegram_name": request[TELEGRAM_NAME],
        "custom_name": await has_chosen_name(db(request), user_id=user.id),
        "role": user.role.value,
        "role_label": ROLE_LABELS[user.role],
        "admin": user.telegram_id in config.admin_ids,
        # Only whether the code is set; the code itself never leaves the server.
        "can_register": user.role == Role.STUDENT
        and bool(config.registration_code.get_secret_value()),
    }


@routes.get("/api/me")
async def me(request: web.Request) -> web.Response:
    return web.json_response(await profile_json(request, current_user(request)))


@routes.put("/api/me/name")
async def rename(request: web.Request) -> web.Response:
    """The name shown to teachers and students instead of the Telegram one."""
    body = await read_body(request, DisplayNameDraft, error=InvalidDisplayName())
    user = await set_display_name(db(request), actor_id=current_user(request).id, name=body.name)
    return web.json_response(await profile_json(request, user))


@routes.delete("/api/me/name")
async def restore_name(request: web.Request) -> web.Response:
    user = await reset_display_name(
        db(request), actor_id=current_user(request).id, telegram_name=request[TELEGRAM_NAME]
    )
    return web.json_response(await profile_json(request, user))


@routes.post("/api/teacher/register")
async def register(request: web.Request) -> web.Response:
    """The teacher code, as «🎓 Я преподаватель» in the chat: same limits and attempt counter."""
    user, config = current_user(request), request.app[CONFIG]
    body = await read_body(request, TeacherCode)
    if not body.code.strip():
        raise InvalidRequest("Введите код преподавателя.")
    try:
        changed = await register_teacher(
            db(request),
            actor_id=user.id,
            code=body.code,
            registration_code=config.registration_code,
        )
    except InvalidRegistrationCode as error:
        # The chat text mentions its «Отмена» button; the page has none.
        raise InvalidRequest(
            f"Неверный код. Осталось попыток: {error.remaining_attempts}."
        ) from None
    # The service changed this very object (one identity map per request session).
    if changed:
        await _switch_chat_menu(request, user)
    return web.json_response({"changed": changed, "me": await profile_json(request, user)})


async def _switch_chat_menu(request: web.Request, user: User) -> None:
    """Give the chat the teacher menu at once and drop a student's unfinished chat input."""
    await chat_state(request, user).clear()
    try:
        await request.app[BOT].send_message(
            user.telegram_id,
            f"Доступ преподавателя открыт. {TEACHER_MENU_HINT}",
            reply_markup=main_menu(Role.TEACHER),
        )
    except TelegramAPIError as error:
        # The role is saved; the old menu buttons in the chat refresh it on the first tap.
        logger.info("Teacher menu was not delivered: %s", type(error).__name__)


@routes.post("/api/admin/grant")
async def grant(request: web.Request) -> web.Response:
    """«👤 Выдать роль преподавателя»: only for ADMIN_IDS, the service checks it."""
    user, config = current_user(request), request.app[CONFIG]
    body = await read_body(request, TeacherGrant)
    target = parse_telegram_id(body.telegram_id)
    if target is None or not body.telegram_id.strip().isdigit():
        raise InvalidRequest("Нужен числовой Telegram ID.")
    changed = await grant_teacher(
        db(request), actor_id=user.id, target_telegram_id=target, admin_ids=config.admin_ids
    )
    return web.json_response({"changed": changed})
