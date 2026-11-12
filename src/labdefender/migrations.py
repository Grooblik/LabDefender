"""Schema migrations of the SQLite database, run by `python -m labdefender.tools migrate`.

States: "legacy" — defense days of one assignment; "slots" — defense slots of one assignment;
"new" — defense slots of a group with queue entries naming the student and the assignment.
Each run makes a backup first and restores it if anything fails.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from labdefender.local import LocalOperationError, database_lock

LEGACY_DAY_START = "09:00:00"
LEGACY_DAY_END = "18:00:00"


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _schema_state(connection: sqlite3.Connection) -> str:
    tables = _tables(connection)
    if not tables:
        return "empty"
    if "assignments" not in tables or "queue_entries" not in tables:
        raise LocalOperationError("Этот SQLite-файл не похож на базу LabDefender.")

    assignment_columns = _columns(connection, "assignments")
    queue_columns = _columns(connection, "queue_entries")
    has_format = "format" in assignment_columns
    has_slots = "defense_slots" in tables
    has_old_days = "defense_days" in tables
    has_slot_id = "slot_id" in queue_columns
    has_defense_day_id = "defense_day_id" in queue_columns

    if has_format and has_slots and has_slot_id and not has_old_days and not has_defense_day_id:
        slot_columns = _columns(connection, "defense_slots")
        if "group_id" in slot_columns and {"student_id", "assignment_id"} <= queue_columns:
            return "new"
        if "assignment_id" in slot_columns and not {"student_id", "assignment_id"} & queue_columns:
            return "slots"
        return "partial"
    if not has_format and not has_slots and has_old_days and has_defense_day_id:
        return "legacy"
    return "partial"


def needs_migration(state: str) -> bool:
    return state in {"legacy", "slots"}


def database_schema_state(path: Path) -> str:
    path = path.resolve()
    if not path.is_file():
        raise LocalOperationError("Файл базы данных не найден.")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as connection:
        return _schema_state(connection)


def _backup(path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    destination = path.parent / "backups" / f"{path.stem}-before-migrate-{timestamp}{path.suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise LocalOperationError("Файл резервной копии уже существует.")
    try:
        with (
            closing(sqlite3.connect(path)) as source,
            closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target, pages=128)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _restore(backup: Path, path: Path) -> None:
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
    for suffix in ("-wal", "-shm"):
        path.with_name(f"{path.name}{suffix}").unlink(missing_ok=True)


def _migrate(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "ALTER TABLE assignments ADD COLUMN format VARCHAR(16) NOT NULL DEFAULT 'online'"
    )
    connection.execute(
        """
        CREATE TABLE defense_slots (
            id INTEGER NOT NULL PRIMARY KEY,
            assignment_id INTEGER NOT NULL,
            date DATE NOT NULL,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            capacity INTEGER NOT NULL,
            is_open BOOLEAN NOT NULL DEFAULT 1,
            CONSTRAINT uq_defense_slots_assignment_id
                UNIQUE (assignment_id, date, start_time),
            CONSTRAINT ck_defense_slots_valid_time_range
                CHECK (start_time < end_time),
            CONSTRAINT ck_defense_slots_positive_capacity
                CHECK (capacity > 0),
            FOREIGN KEY(assignment_id) REFERENCES assignments (id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO defense_slots
            (id, assignment_id, date, start_time, end_time, capacity, is_open)
        SELECT id, assignment_id, date, ?, ?, capacity, 1
        FROM defense_days
        """,
        (LEGACY_DAY_START, LEGACY_DAY_END),
    )
    connection.execute(
        "CREATE INDEX ix_defense_slots_assignment_id ON defense_slots (assignment_id)"
    )
    connection.execute(
        """
        CREATE TABLE queue_entries_new (
            id INTEGER NOT NULL PRIMARY KEY,
            slot_id INTEGER NOT NULL,
            submission_id INTEGER NOT NULL,
            order_at DATETIME NOT NULL,
            FOREIGN KEY(slot_id) REFERENCES defense_slots (id),
            FOREIGN KEY(submission_id) REFERENCES submissions (id),
            UNIQUE (submission_id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO queue_entries_new (id, slot_id, submission_id, order_at)
        SELECT id, defense_day_id, submission_id, order_at
        FROM queue_entries
        """
    )
    connection.execute("CREATE INDEX ix_queue_entries_slot_id ON queue_entries_new (slot_id)")
    connection.execute("DROP TABLE queue_entries")
    connection.execute("ALTER TABLE queue_entries_new RENAME TO queue_entries")
    connection.execute("DROP TABLE defense_days")
    connection.commit()


def _count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


def _migrate_group_queue(connection: sqlite3.Connection) -> None:
    """Slots move from an assignment to its group; entries also store student and assignment."""
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    slots, entries = _count(connection, "defense_slots"), _count(connection, "queue_entries")
    connection.execute(
        """
        CREATE TABLE defense_slots_new (
            id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            date DATE NOT NULL,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            capacity INTEGER NOT NULL,
            is_open BOOLEAN DEFAULT '1' NOT NULL,
            CONSTRAINT pk_defense_slots PRIMARY KEY (id),
            CONSTRAINT ck_defense_slots_valid_time_range CHECK (start_time < end_time),
            CONSTRAINT ck_defense_slots_positive_capacity CHECK (capacity > 0),
            CONSTRAINT fk_defense_slots_group_id_study_groups
                FOREIGN KEY(group_id) REFERENCES study_groups (id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO defense_slots_new (id, group_id, date, start_time, end_time, capacity, is_open)
        SELECT slot.id, assignment.group_id, slot.date, slot.start_time, slot.end_time,
               slot.capacity, slot.is_open
        FROM defense_slots AS slot
        JOIN assignments AS assignment ON assignment.id = slot.assignment_id
        """
    )
    connection.execute(
        """
        CREATE TABLE queue_entries_new (
            id INTEGER NOT NULL,
            slot_id INTEGER NOT NULL,
            submission_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            assignment_id INTEGER NOT NULL,
            order_at DATETIME NOT NULL,
            CONSTRAINT pk_queue_entries PRIMARY KEY (id),
            CONSTRAINT uq_queue_entries_student_id UNIQUE (student_id, assignment_id),
            CONSTRAINT fk_queue_entries_slot_id_defense_slots
                FOREIGN KEY(slot_id) REFERENCES defense_slots (id),
            CONSTRAINT uq_queue_entries_submission_id UNIQUE (submission_id),
            CONSTRAINT fk_queue_entries_submission_id_submissions
                FOREIGN KEY(submission_id) REFERENCES submissions (id),
            CONSTRAINT fk_queue_entries_student_id_users
                FOREIGN KEY(student_id) REFERENCES users (id),
            CONSTRAINT fk_queue_entries_assignment_id_assignments
                FOREIGN KEY(assignment_id) REFERENCES assignments (id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO queue_entries_new
            (id, slot_id, submission_id, student_id, assignment_id, order_at)
        SELECT entry.id, entry.slot_id, entry.submission_id, submission.student_id,
               submission.assignment_id, entry.order_at
        FROM queue_entries AS entry
        JOIN submissions AS submission ON submission.id = entry.submission_id
        """
    )
    if (_count(connection, "defense_slots_new"), _count(connection, "queue_entries_new")) != (
        slots,
        entries,
    ):
        raise LocalOperationError("Миграция очереди потеряла бы записи. База не изменена.")
    connection.execute("DROP TABLE queue_entries")
    connection.execute("DROP TABLE defense_slots")
    connection.execute("ALTER TABLE defense_slots_new RENAME TO defense_slots")
    connection.execute("ALTER TABLE queue_entries_new RENAME TO queue_entries")
    connection.execute("CREATE INDEX ix_defense_slots_group_id ON defense_slots (group_id)")
    connection.execute("CREATE INDEX ix_queue_entries_slot_id ON queue_entries (slot_id)")
    connection.execute(
        "CREATE INDEX ix_queue_entries_assignment_id ON queue_entries (assignment_id)"
    )
    connection.commit()


def _verify(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise LocalOperationError("Проверка целостности после миграции не пройдена.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise LocalOperationError("После миграции нарушены связи между таблицами.")
    if _schema_state(connection) != "new":
        raise LocalOperationError("Схема после миграции не соответствует ожидаемой.")


def migrate_database(path: Path) -> tuple[Path, Path | None]:
    path = path.resolve()
    if not path.is_file():
        raise LocalOperationError("Файл базы данных не найден.")
    with database_lock(path):
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as connection:
            state = _schema_state(connection)
        if state in {"new", "empty"}:
            return path, None
        if not needs_migration(state):
            raise LocalOperationError(
                "База частично мигрирована или имеет неизвестную схему. "
                "Восстановите её из резервной копии."
            )
        backup = _backup(path)
        try:
            with closing(sqlite3.connect(path)) as connection:
                if state == "legacy":
                    _migrate(connection)
                _migrate_group_queue(connection)
                _verify(connection)
        except BaseException as error:
            _restore(backup, path)
            raise LocalOperationError(
                f"Миграция не удалась. База восстановлена из копии: {backup}"
            ) from error
        return path, backup
