import asyncio
import logging

from pydantic import ValidationError
from pydantic_settings import SettingsError

from labdefender.app import run
from labdefender.config import Settings
from labdefender.local import LocalOperationError


def main() -> None:
    try:
        settings = Settings()
    except (ValidationError, SettingsError):
        # ValidationError can contain the input (including the token).
        raise SystemExit(
            "Проверьте BOT_TOKEN, DATABASE_URL, LOG_LEVEL, "
            "TEACHER_REGISTRATION_CODE и ADMIN_IDS в .env."
        ) from None

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    except LocalOperationError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
