import argparse
import asyncio
import logging
from collections.abc import Sequence

from pydantic import ValidationError
from pydantic_settings import SettingsError

from labdefender.app import run
from labdefender.config import Settings
from labdefender.local import LocalOperationError
from labdefender.tunnel import local_web_url, permanent_tunnel, quick_tunnel, running_tunnel
from labdefender.web import mini_app_url


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Запуск бота LabDefender")
    result.add_argument(
        "--tunnel",
        action="store_true",
        help=(
            "Открыть Mini App через туннель Cloudflare (нужен cloudflared): постоянный, "
            "если в .env заданы TUNNEL_TOKEN и WEBAPP_URL, иначе временный"
        ),
    )
    return result


def run_with_tunnel(settings: Settings) -> None:
    token = settings.tunnel_token.get_secret_value()
    if token:
        if not settings.webapp_url:
            raise LocalOperationError(
                "Для постоянного туннеля задайте в .env WEBAPP_URL — адрес https://, "
                "который в Cloudflare ведёт на этот туннель."
            )
        with permanent_tunnel(token, settings.webapp_url):
            print(
                f"Mini App: {mini_app_url(settings.webapp_url)} (постоянный адрес).\n"
                "Бот сам проверит, открывается ли адрес снаружи, и напишет результат ниже."
            )
            asyncio.run(run(settings))
        return
    local_url = local_web_url(settings.web_host, settings.web_port)
    kept = running_tunnel(local_url)
    if kept:
        print(
            f"Mini App: {mini_app_url(kept)}\n"
            "Адрес взят у туннеля в отдельном окне: перезапуск бота его не меняет."
        )
        asyncio.run(run(settings.model_copy(update={"webapp_url": kept})))
        return
    with quick_tunnel(local_url) as tunnel:
        print(
            f"Mini App: {mini_app_url(tunnel.url)}\n"
            "Временный адрес меняется при каждом запуске. Telegram обновляет кнопку "
            "«Приложение» с задержкой до пары минут: если открылась ошибка Cloudflare "
            "1033 (прежний адрес), подождите и откройте чат с ботом заново.\n"
            "Чтобы адрес переживал перезапуск бота, держите туннель в отдельном окне: "
            "sh scripts/tunnel.sh (Windows: .\\scripts\\tunnel.ps1)."
        )
        asyncio.run(run(settings.model_copy(update={"webapp_url": tunnel.url})))


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        settings = Settings()
    except (ValidationError, SettingsError):
        # ValidationError can contain the input (including the token).
        raise SystemExit(
            "Проверьте BOT_TOKEN, DATABASE_URL, LOG_LEVEL, TEACHER_REGISTRATION_CODE, "
            "ADMIN_IDS, WEBAPP_URL, WEB_HOST, WEB_PORT, WEBAPP_AUTH_MAX_AGE и TUNNEL_TOKEN в .env."
        ) from None

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.tunnel:
            run_with_tunnel(settings)
        else:
            asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    except LocalOperationError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
