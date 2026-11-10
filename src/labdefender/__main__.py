import asyncio
import logging

from pydantic import ValidationError

from labdefender.app import run
from labdefender.config import Settings


def main() -> None:
    try:
        settings = Settings()
    except ValidationError:
        # ValidationError can contain the input (including the token).
        raise SystemExit("Проверьте BOT_TOKEN, DATABASE_URL и LOG_LEVEL в .env.") from None

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
