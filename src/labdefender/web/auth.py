"""Mini App authentication: every API request carries Telegram initData signed with the bot token.

The client sends `Authorization: tma <initData>`. The signature proves that Telegram issued the
data for this bot; the user ID is taken only from it, never from the request body or URL.
"""

from datetime import UTC, datetime, timedelta

from aiogram.utils.web_app import WebAppInitData, safe_parse_webapp_init_data

# Allow a small clock difference between Telegram and this computer.
CLOCK_SKEW = timedelta(minutes=5)


class InitDataError(Exception):
    """The request is not authenticated; the message is safe to show to the user."""


def read_init_data(
    header: str | None, *, bot_token: str, max_age: int, now: datetime | None = None
) -> WebAppInitData:
    scheme, _, value = (header or "").partition(" ")
    if scheme != "tma" or not value:
        raise InitDataError("Откройте приложение из Telegram.")
    try:
        data = safe_parse_webapp_init_data(token=bot_token, init_data=value)
    except ValueError:
        raise InitDataError(
            "Telegram не подтвердил вход. Закройте и откройте приложение заново."
        ) from None
    if data.user is None:
        raise InitDataError("Откройте приложение из личного чата с ботом.")
    age = (now or datetime.now(UTC)) - data.auth_date
    if age > timedelta(seconds=max_age) or age < -CLOCK_SKEW:
        raise InitDataError("Сеанс устарел. Закройте и откройте приложение заново.")
    return data
