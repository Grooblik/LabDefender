"""Start-up check that the public Mini App address really leads to this process.

Phones open the Mini App through the tunnel, never through 127.0.0.1, so a wrong tunnel setting
(another port, a mistyped WEBAPP_URL, a backup laptop that is still running) is only visible
from outside. The bot asks its own health endpoint through the public address and writes
the result to the console. The check never stops the bot.
"""

import asyncio
import logging
import secrets
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp
import certifi

logger = logging.getLogger(__name__)

HEALTH_PATH = "/health"
ATTEMPTS = 12
DELAY = 5.0
TIMEOUT = 10.0


def new_instance() -> str:
    return secrets.token_hex(8)


def health_url(webapp_url: str) -> str:
    return f"{webapp_url}{HEALTH_PATH}"


async def check_public_address(
    url: str,
    instance: str,
    *,
    attempts: int = ATTEMPTS,
    delay: float = DELAY,
    request_timeout: float = TIMEOUT,
) -> bool:
    """Ask `url` for the health answer until it names `instance`. Logs the outcome."""
    # Some Python builds lack system certificates; aiogram relies on certifi the same way.
    context = ssl.create_default_context(cafile=certifi.where())
    problem = "нет ответа"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=request_timeout)) as http:
        for attempt in range(attempts):
            if attempt:
                # A new tunnel needs a few seconds before Cloudflare routes requests to it.
                await asyncio.sleep(delay)
            try:
                async with http.get(url, ssl=context, allow_redirects=False) as response:
                    if response.status != 200:
                        problem = f"ответ HTTP {response.status}"
                        continue
                    data = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                problem = f"ошибка соединения {type(error).__name__}"
                continue
            if not isinstance(data, dict) or data.get("app") != "labdefender":
                problem = "по адресу отвечает не LabDefender"
                continue
            if data.get("instance") != instance:
                logger.warning(
                    "По адресу Mini App отвечает другой запуск LabDefender. Остановите его "
                    "(например, на резервном ноутбуке): два запуска делят один адрес, "
                    "и часть студентов попадёт в чужую базу."
                )
                return False
            logger.info("Mini App открывается по публичному адресу %s", url)
            return True
    logger.warning(
        "Mini App не открылась по публичному адресу %s (%s). Проверьте, что туннель запущен "
        "и ведёт на этот компьютер (http://127.0.0.1:<WEB_PORT>), а WEBAPP_URL совпадает "
        "с адресом туннеля. Бот в чате работает и без Mini App.",
        url,
        problem,
    )
    return False


@asynccontextmanager
async def address_check(webapp_url: str, instance: str | None) -> AsyncIterator[None]:
    """Run the check in the background while the bot works; cancel it on shutdown."""
    if not webapp_url or not instance:
        yield
        return
    task = asyncio.create_task(check_public_address(health_url(webapp_url), instance))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
