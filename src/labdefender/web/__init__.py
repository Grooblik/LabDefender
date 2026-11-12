"""Telegram Mini App: HTTP API and static page served next to the polling bot, in one process.

Kept free of heavy imports: chat handlers import `mini_app_url` from here.
"""

import re
from urllib.parse import quote

# A screen of the page, as in its own navigation: "/review/s/42". Digits are object IDs.
SCREEN = re.compile(r"(?:/[a-z]+(?:/\d+)?)+")


def mini_app_url(webapp_url: str, screen: str | None = None) -> str:
    """Address of the page; with a screen, the page opens it instead of the home screen.

    The screen travels in the query string: Telegram puts its launch data into the fragment.
    """
    if screen is None or screen == "/":
        return f"{webapp_url}/app/"
    if not SCREEN.fullmatch(screen):
        raise ValueError(f"Unknown Mini App screen: {screen!r}")
    return f"{webapp_url}/app/?screen={quote(screen, safe='/')}"
