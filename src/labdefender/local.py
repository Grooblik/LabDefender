import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from filelock import FileLock, Timeout
from sqlalchemy.engine import make_url

from labdefender.db import models  # noqa: F401 -- populate metadata
from labdefender.db.base import Base


class LocalOperationError(RuntimeError):
    """Local maintenance failure safe to show without dumping settings or tokens."""


def database_path(url: str) -> Path:
    parsed = make_url(url)
    if (
        parsed.drivername != "sqlite+aiosqlite"
        or not parsed.database
        or parsed.database == ":memory:"
    ):
        raise LocalOperationError(
            "Для локального запуска и резервирования нужна файловая SQLite-база."
        )
    if parsed.query:
        raise LocalOperationError(
            "В локальном режиме DATABASE_URL должен содержать обычный путь без query-параметров."
        )
    return Path(parsed.database).expanduser().resolve()


@contextmanager
def database_lock(path: Path) -> Iterator[None]:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path.with_name(f".{path.name}.labdefender.lock")))
    try:
        with lock.acquire(timeout=0):
            yield
    except Timeout:
        raise LocalOperationError(
            "С этой базой уже работает другой локальный процесс. Сначала остановите его."
        ) from None


@contextmanager
def instance_locks(path: Path, *, bot_id: int) -> Iterator[None]:
    # A local-user lock shared by different checkouts. No token is written to disk.
    lock_dir = Path(tempfile.gettempdir()) / "labdefender-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_dir / f"bot-{bot_id}.labdefender.lock"))
    with database_lock(path):
        try:
            with lock.acquire(timeout=0):
                yield
        except Timeout:
            raise LocalOperationError(
                "Этот бот уже запущен на этом компьютере. Закройте другой процесс."
            ) from None


# Tables added after the first schema. `create_all` adds them at the next bot start, so an
# older database without them is still a valid LabDefender database for backup and restore.
ADDITIVE_TABLES = frozenset(
    {"teacher_registration_attempts", "notification_settings", "user_profiles"}
)


def check_database(path: Path, *, allow_missing_tables: bool = False) -> tuple[str, ...]:
    path = path.resolve()
    if not path.is_file():
        raise LocalOperationError("Файл базы данных не найден.")
    try:
        # mode=rw never creates a missing database, but allows SQLite to recover a hot journal.
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise LocalOperationError("Проверка целостности SQLite не пройдена.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise LocalOperationError("Нарушены связи между таблицами SQLite.")
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = []
            if tables and not tables.intersection(Base.metadata.tables):
                raise LocalOperationError("Этот SQLite-файл не похож на базу LabDefender.")
            for table in Base.metadata.sorted_tables:
                if table.name not in tables:
                    missing.append(table.name)
                    continue
                columns = {
                    row[1] for row in connection.execute(f'PRAGMA table_info("{table.name}")')
                }
                if not set(table.columns.keys()) <= columns:
                    raise LocalOperationError(
                        f"Схема таблицы {table.name} устарела: отсутствуют нужные столбцы."
                    )
            if missing and not allow_missing_tables and not set(missing) <= ADDITIVE_TABLES:
                raise LocalOperationError(
                    "В базе отсутствуют таблицы LabDefender. Проверьте версию проекта."
                )
            return tuple(missing)
    except sqlite3.DatabaseError:
        raise LocalOperationError("Не удалось прочитать корректную SQLite-базу.") from None


def reserve_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise LocalOperationError(
            "Файл назначения уже существует. Укажите новый путь; данные не перезаписаны."
        ) from None
    os.close(descriptor)


def backup_database(source: Path, destination: Path) -> Path:
    source, destination = source.resolve(), destination.resolve()
    check_database(source)
    reserve_database(destination)
    try:
        # SQLite's online backup includes committed WAL contents; no raw file copy.
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as backup:
                original.backup(backup, pages=128)
        check_database(destination)
    except BaseException:
        # This call exclusively created the destination; existing files are never removed.
        destination.unlink(missing_ok=True)
        raise
    return destination


def restore_database(source: Path, destination: Path) -> Path:
    with database_lock(destination):
        return backup_database(source, destination)
