import argparse
import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy import select

from labdefender.config import DatabaseSettings, Settings
from labdefender.db.models import Submission, User
from labdefender.db.session import create_database
from labdefender.demo import DEFAULT_STUDENT_IDS, DEFAULT_TEACHER_ID, seed_demo
from labdefender.enums import SubmissionStatus
from labdefender.local import (
    LocalOperationError,
    backup_database,
    check_database,
    database_path,
    restore_database,
)
from labdefender.migrations import database_schema_state, migrate_database
from labdefender.services.queue import get_student_booking
from labdefender.tunnel import TUNNEL_URL, local_web_url, shared_tunnel
from labdefender.web import mini_app_url


async def telegram_check(settings: Settings) -> None:
    try:
        async with Bot(token=settings.bot_token.get_secret_value()) as bot:
            me = await bot.get_me()
            webhook = await bot.get_webhook_info()
    except TelegramAPIError:
        raise LocalOperationError(
            "Не удалось проверить Telegram. Проверьте токен и интернет-соединение."
        ) from None
    if webhook.url:
        raise LocalOperationError(
            "У бота включён webhook. Для локального polling нужен отдельный бот без webhook."
        )
    print(f"OK: Telegram @{me.username}, ID {me.id}. Сообщения не отправлялись.")


def doctor(*, online: bool = False) -> None:
    settings = Settings()
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise LocalOperationError(
            "Нужен Python со SQLite 3.35+ (поддержка RETURNING). Обновите Python."
        )
    path = database_path(settings.database_url)
    print(f"OK: Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}.")
    print("OK: настройки прочитаны; секретные значения скрыты.")
    if path.exists():
        state = database_schema_state(path)
        if state == "legacy":
            print(
                "INFO: найдена старая схема. Выполните "
                "`python -m labdefender.tools migrate`, чтобы сохранить данные."
            )
        elif state == "partial":
            print("WARN: схема базы частично мигрирована или неизвестна. Проверьте копию.")
        else:
            missing = check_database(path, allow_missing_tables=True)
            print("OK: целостность, связи и столбцы существующих таблиц SQLite.")
            if missing:
                print(f"INFO: при запуске будут созданы таблицы: {', '.join(missing)}.")
    else:
        print("INFO: база ещё не создана; она появится при первом запуске.")
    print(f"База: {path}")
    if not settings.admin_ids and not settings.teacher_registration_code.get_secret_value():
        print("INFO: задайте ADMIN_IDS или TEACHER_REGISTRATION_CODE для доступа преподавателя.")
    mini_app_doctor(settings)
    if online:
        asyncio.run(telegram_check(settings))
    else:
        print("INFO: Telegram не проверялся. Для проверки соединения добавьте --telegram.")


def mini_app_doctor(settings: Settings) -> None:
    token = bool(settings.tunnel_token.get_secret_value())
    if settings.webapp_url:
        print(
            f"OK: Mini App {mini_app_url(settings.webapp_url)}, "
            f"сервер {settings.web_host}:{settings.web_port}."
        )
        if TUNNEL_URL.fullmatch(settings.webapp_url):
            print(
                "WARN: WEBAPP_URL — временный адрес trycloudflare.com; он перестаёт работать "
                "после перезапуска туннеля. Для временного адреса оставьте WEBAPP_URL пустым "
                "и запускайте с --tunnel."
            )
    else:
        print("INFO: Mini App выключена (WEBAPP_URL пуст); временный адрес даёт запуск с --tunnel.")
    if token and not settings.webapp_url:
        raise LocalOperationError(
            "TUNNEL_TOKEN задан, а WEBAPP_URL пуст: укажите адрес https://, который в Cloudflare "
            "ведёт на этот туннель."
        )
    if token:
        print(
            "OK: постоянный туннель Cloudflare: запуск с --tunnel поднимет его; "
            "в Cloudflare адрес должен вести на "
            f"{local_web_url(settings.web_host, settings.web_port)}."
        )
    if (token or not settings.webapp_url) and shutil.which("cloudflared") is None:
        print("INFO: cloudflared не установлен; он нужен для запуска с --tunnel.")


def keep_tunnel() -> None:
    """Hold a quick tunnel in this window; bots started with --tunnel take its address."""
    settings = Settings()
    if settings.tunnel_token.get_secret_value():
        raise LocalOperationError(
            "TUNNEL_TOKEN задан: постоянный туннель поднимает сам запуск бота с --tunnel, "
            "отдельное окно для него не нужно."
        )
    with shared_tunnel(local_web_url(settings.web_host, settings.web_port)) as tunnel:
        print(
            f"Временный туннель запущен. Mini App: {mini_app_url(tunnel.url)}\n"
            "Оставьте это окно открытым и запустите бота с --tunnel в другом окне: он возьмёт "
            "этот адрес, и перезапуск бота адрес не изменит.\n"
            "Ctrl+C — остановить туннель; после этого адрес перестанет работать.",
            flush=True,
        )
        while True:
            try:
                tunnel.process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                continue  # Short waits keep Ctrl+C responsive on every platform.
    raise LocalOperationError(
        "cloudflared остановился, адрес больше не работает. Запустите туннель заново: "
        "бот возьмёт новый адрес при следующем запуске с --tunnel."
    )


async def offline_smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="labdefender-smoke-") as directory:
        root = Path(directory)
        source = root / "demo.sqlite3"
        summary = await seed_demo(
            source, teacher_id=DEFAULT_TEACHER_ID, student_ids=DEFAULT_STUDENT_IDS
        )
        backup = backup_database(source, root / "backup.sqlite3")
        restored = restore_database(backup, root / "restored.sqlite3")
        engine, sessions = create_database(f"sqlite+aiosqlite:///{restored}")
        try:
            async with sessions() as session:
                student = await session.scalar(
                    select(User).where(User.telegram_id == DEFAULT_STUDENT_IDS[0])
                )
                booking = await get_student_booking(
                    session, actor_id=student.id, assignment_id=summary["assignment_ids"][0]
                )
                statuses = set((await session.scalars(select(Submission.status))).all())
                if (
                    booking is None
                    or booking.position != 1
                    or booking.occupied != 2
                    or statuses
                    != {
                        SubmissionStatus.PENDING,
                        SubmissionStatus.RETURNED,
                        SubmissionStatus.APPROVED,
                    }
                ):
                    raise LocalOperationError("Локальный smoke-тест не прошёл.")
        finally:
            await engine.dispose()
    print(
        "OK: роли, группа, задания, сдача, возврат, допуск, очередь, "
        "backup и restore. Без Telegram."
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Локальные инструменты LabDefender")
    commands = result.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor", help="Проверить настройки и SQLite")
    check.add_argument(
        "--telegram", action="store_true", help="Прочитать сведения о боте через Telegram API"
    )
    backup = commands.add_parser("backup", help="Сделать согласованную SQLite-копию")
    backup.add_argument("--database", type=Path, help="Исходный SQLite-файл; по умолчанию из .env")
    backup.add_argument(
        "--output", type=Path, help="Новый файл; по умолчанию backups/<UTC-время>.sqlite3"
    )
    restore = commands.add_parser("restore", help="Восстановить копию в новый файл")
    restore.add_argument("source", type=Path)
    restore.add_argument("--output", type=Path, required=True)
    migrate = commands.add_parser(
        "migrate", help="Мигрировать старую базу к форматам заданий и слотам"
    )
    migrate.add_argument("--database", type=Path, help="SQLite-файл; по умолчанию из .env")
    seed = commands.add_parser("seed-demo", help="Создать отдельную новую демобазу")
    seed.add_argument("--output", type=Path, default=Path("data/demo.sqlite3"))
    seed.add_argument("--teacher-id", type=int, required=True)
    seed.add_argument("--student-id", type=int, action="append", required=True)
    commands.add_parser(
        "smoke", help="Пройти локальный сценарий во временной базе, без токена и сети"
    )
    commands.add_parser(
        "tunnel",
        help="Держать временный туннель Mini App в отдельном окне (адрес переживает рестарт бота)",
    )
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "doctor":
            doctor(online=args.telegram)
        elif args.command == "smoke":
            asyncio.run(offline_smoke())
        elif args.command == "tunnel":
            try:
                keep_tunnel()
            except KeyboardInterrupt:
                print("Туннель остановлен.")
        elif args.command == "seed-demo":
            data = asyncio.run(
                seed_demo(
                    args.output,
                    teacher_id=args.teacher_id,
                    student_ids=tuple(args.student_id),
                )
            )
            print(json.dumps(data, ensure_ascii=False, indent=2))
            print(
                "Демобаза создана отдельно. Для Telegram используйте реальные ID "
                "и выберите этот файл в DATABASE_URL."
            )
        elif args.command == "migrate":
            source = args.database or database_path(DatabaseSettings().database_url)
            migrated, backup = migrate_database(source)
            print(f"Миграция завершена: {migrated}")
            if backup is not None:
                print(f"Резервная копия: {backup}")
            else:
                print("INFO: миграция не требуется.")
        elif args.command == "backup":
            source = args.database or database_path(DatabaseSettings().database_url)
            destination = args.output or Path("backups") / datetime.now(UTC).strftime(
                "labdefender-%Y%m%d-%H%M%S-%f.sqlite3"
            )
            print(f"Копия готова: {backup_database(source, destination)}")
        else:
            restored = restore_database(args.source, args.output)
            print(f"Восстановлено в новый файл: {restored}")
            print(
                "Остановите бота, выберите этот путь в DATABASE_URL, "
                "выполните doctor и запустите снова."
            )
    except (ValidationError, SettingsError):
        raise SystemExit(
            "Проверьте настройки в .env. ADMIN_IDS должен быть JSON-массивом; секреты не выводятся."
        ) from None
    except LocalOperationError as error:
        raise SystemExit(str(error)) from None
    except (OSError, sqlite3.DatabaseError):
        raise SystemExit(
            "Локальная операция не выполнена. Проверьте пути, права доступа и свободное место."
        ) from None


if __name__ == "__main__":
    main()
