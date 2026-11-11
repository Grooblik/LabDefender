"""Keys of the Mini App web application and request helpers shared by the API modules."""

from dataclasses import dataclass
from typing import TypeVar

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiohttp import web
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from labdefender.db.models import User
from labdefender.handlers.notifications import Notifier
from labdefender.services.errors import (
    AccessDenied,
    DomainError,
    InvalidAssignment,
    InvalidAssignmentFormat,
    InvalidDefenseSlot,
    InvalidDisplayName,
    InvalidGroupName,
    InvalidInvitation,
    InvalidRegistrationCode,
    InvalidReviewComment,
    InvalidSolution,
    ObjectUnavailable,
    RegistrationBlocked,
)
from labdefender.services.pagination import Page, validate_offset
from labdefender.web.idempotency import IdempotencyCache, InvalidIdempotencyKey

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class WebConfig:
    bot_token: SecretStr
    auth_max_age: int
    admin_ids: frozenset[int] = frozenset()
    registration_code: SecretStr = SecretStr("")
    # Random per process: the start-up check tells this process from another one at the address.
    instance: str = ""


SESSIONS = web.AppKey("sessions", async_sessionmaker[AsyncSession])
CONFIG = web.AppKey("config", WebConfig)
BOT = web.AppKey("bot", Bot)
NOTIFIER = web.AppKey("notifier", Notifier)
IDEMPOTENCY = web.AppKey("idempotency", IdempotencyCache)
# The bot's FSM storage: a Mini App button can start a step that continues in the chat.
STORAGE = web.AppKey("storage", BaseStorage)

# Request keys filled by the API middleware for authenticated handlers. aiohttp 3.14+ prefers
# typed keys; aiogram also allows older aiohttp, where plain strings are the only option.
_RequestKey = getattr(web, "RequestKey", None)
SESSION = _RequestKey("session", AsyncSession) if _RequestKey else "session"
USER = _RequestKey("user", User) if _RequestKey else "user"
# The name from the signed Telegram data of this request, even when the user chose another.
TELEGRAM_NAME = _RequestKey("telegram_name", str) if _RequestKey else "telegram_name"


class InvalidRequest(DomainError):
    def __init__(self, message: str = "") -> None:
        super().__init__(message or "Некорректный запрос. Обновите приложение и попробуйте снова.")


class ChatUnavailable(DomainError):
    def __init__(self) -> None:
        super().__init__("Не удалось написать вам в чат с ботом. Откройте чат и попробуйте снова.")


VALIDATION_ERRORS = (
    InvalidRequest,
    InvalidIdempotencyKey,
    InvalidDefenseSlot,
    InvalidAssignment,
    InvalidAssignmentFormat,
    InvalidSolution,
    InvalidReviewComment,
    InvalidGroupName,
    InvalidDisplayName,
    InvalidInvitation,
    InvalidRegistrationCode,
)


def error_status(error: DomainError) -> int:
    """403 no rights, 404 not found, 422 invalid input, 429 too many attempts, else 409."""
    if isinstance(error, AccessDenied):
        return 403
    if isinstance(error, ObjectUnavailable):
        return 404
    if isinstance(error, RegistrationBlocked):
        return 429
    if isinstance(error, VALIDATION_ERRORS):
        return 422
    return 409


def json_error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def current_user(request: web.Request) -> User:
    return request[USER]


def db(request: web.Request) -> AsyncSession:
    return request[SESSION]


def path_id(request: web.Request, name: str = "id") -> int:
    # Routes accept digits only; larger values than a database ID are simply not found.
    value = int(request.match_info[name])
    if value > 2**63 - 1:
        raise ObjectUnavailable()
    return value


def query_int(request: web.Request, name: str, default: int = 0) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit() or len(raw) > 10:
        raise InvalidRequest()
    return int(raw)


def query_offset(request: web.Request) -> int:
    offset = query_int(request, "offset")
    validate_offset(offset)
    return offset


async def read_body(request: web.Request, model: type[M], *, error: DomainError | None = None) -> M:
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):  # Invalid JSON is a ValueError too.
        raise error or InvalidRequest() from None


def page_json(page: Page, items: list[dict]) -> dict:
    return {"items": items, "offset": page.offset, "has_next": page.has_next}


def chat_state(request: web.Request, user: User) -> FSMContext:
    """The FSM context of the user's private chat with the bot, as the dispatcher builds it."""
    bot = request.app[BOT]
    key = StorageKey(bot_id=bot.id, chat_id=user.telegram_id, user_id=user.telegram_id)
    return FSMContext(storage=request.app[STORAGE], key=key)
