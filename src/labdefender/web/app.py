import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import MenuButtonDefault, MenuButtonWebApp, WebAppInfo
from aiohttp import web
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from labdefender.config import Settings
from labdefender.handlers.notifications import Notifier
from labdefender.local import LocalOperationError
from labdefender.services.errors import DomainError
from labdefender.services.users import ensure_user
from labdefender.web import mini_app_url
from labdefender.web.api import add_routes
from labdefender.web.auth import InitDataError, read_init_data
from labdefender.web.context import (
    BOT,
    CONFIG,
    IDEMPOTENCY,
    NOTIFIER,
    SESSION,
    SESSIONS,
    STORAGE,
    TELEGRAM_NAME,
    USER,
    WebConfig,
    error_status,
    json_error,
)
from labdefender.web.idempotency import IdempotencyCache
from labdefender.web.reachability import HEALTH_PATH, new_instance

__all__ = ["configure_menu_button", "create_web_app", "mini_app_url", "web_server"]

logger = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
MENU_BUTTON_TEXT = "Приложение"
# telegram-web-app.js is loaded from telegram.org; the API is same-origin.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
)
# Module scripts need a JavaScript type; never depend on the platform's MIME table.
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
INTERNAL_ERROR = "Не удалось выполнить действие. Попробуйте ещё раз."

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def security_headers(request: web.Request, handler: Handler) -> web.StreamResponse:
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    else:
        # Telegram WebViews cache aggressively: always revalidate the page and its files.
        response.headers.setdefault("Cache-Control", "no-cache")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        content_type = STATIC_TYPES.get(Path(request.path).suffix)
        if content_type and isinstance(response, web.FileResponse):
            response.headers["Content-Type"] = content_type
    return response


@web.middleware
async def api_errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Domain errors become JSON with the same text as in the chat; nothing else leaks."""
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        return await handler(request)
    except DomainError as error:
        return json_error(str(error), error_status(error))
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Mini App API failed: %s %s", request.method, request.path)
        return json_error(INTERNAL_ERROR, 500)


@web.middleware
async def api_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Authenticate every /api/ request and give the handler its own database session."""
    if not request.path.startswith("/api/"):
        return await handler(request)
    config = request.app[CONFIG]
    try:
        data = read_init_data(
            request.headers.get("Authorization"),
            bot_token=config.bot_token.get_secret_value(),
            max_age=config.auth_max_age,
        )
    except InitDataError as error:
        return json_error(str(error), 401)
    person = data.user
    full_name = " ".join(part for part in (person.first_name, person.last_name) if part)
    # A separate session per request, as for bot updates. Uncommitted work rolls back on close.
    async with request.app[SESSIONS]() as session:
        request[SESSION] = session
        request[TELEGRAM_NAME] = full_name or str(person.id)
        request[USER] = await ensure_user(
            session,
            telegram_id=person.id,
            username=person.username,
            full_name=request[TELEGRAM_NAME],
        )
        return await handler(request)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def health(request: web.Request) -> web.Response:
    # Public and harmless: lets the start-up check recognise this very process.
    return web.json_response({"app": "labdefender", "instance": request.app[CONFIG].instance})


def create_web_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    bot_token: SecretStr,
    auth_max_age: int,
    bot: Bot,
    notifier: Notifier,
    storage: BaseStorage,
    admin_ids: frozenset[int] = frozenset(),
    registration_code: SecretStr | None = None,
    instance: str | None = None,
    idempotency: IdempotencyCache | None = None,
) -> web.Application:
    app = web.Application(middlewares=[security_headers, api_errors, api_auth])
    app[SESSIONS] = session_factory
    app[CONFIG] = WebConfig(
        bot_token=bot_token,
        auth_max_age=auth_max_age,
        admin_ids=admin_ids,
        registration_code=registration_code or SecretStr(""),
        instance=instance or new_instance(),
    )
    app[BOT] = bot
    app[NOTIFIER] = notifier
    app[STORAGE] = storage
    app[IDEMPOTENCY] = idempotency or IdempotencyCache()
    app.router.add_get("/app/", index)
    app.router.add_get(HEALTH_PATH, health)
    app.router.add_static("/app/", STATIC)
    add_routes(app)
    return app


@asynccontextmanager
async def web_server(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    bot: Bot,
    notifier: Notifier,
    storage: BaseStorage,
) -> AsyncIterator[str | None]:
    """Serve the Mini App while the bot polls. Without WEBAPP_URL nothing is started.

    Yields the ID of this process that the health endpoint reports, or None when off.
    """
    if not settings.webapp_url:
        yield None
        return
    instance = new_instance()
    runner = web.AppRunner(
        create_web_app(
            session_factory,
            bot_token=settings.bot_token,
            auth_max_age=settings.webapp_auth_max_age,
            bot=bot,
            notifier=notifier,
            storage=storage,
            admin_ids=settings.admin_ids,
            registration_code=settings.teacher_registration_code,
            instance=instance,
        )
    )
    await runner.setup()
    try:
        try:
            await web.TCPSite(runner, settings.web_host, settings.web_port).start()
        except OSError:
            raise LocalOperationError(
                f"Не удалось открыть {settings.web_host}:{settings.web_port} для Mini App. "
                "Порт занят другой программой — освободите его или задайте WEB_PORT."
            ) from None
        logger.info("Mini App is served at %s", mini_app_url(settings.webapp_url))
        yield instance
    finally:
        await runner.cleanup()


async def configure_menu_button(bot: Bot, webapp_url: str) -> None:
    """The button next to the input field opens the Mini App; without it, the commands list."""
    button = (
        MenuButtonWebApp(text=MENU_BUTTON_TEXT, web_app=WebAppInfo(url=mini_app_url(webapp_url)))
        if webapp_url
        else MenuButtonDefault()
    )
    await bot.set_chat_menu_button(menu_button=button)
