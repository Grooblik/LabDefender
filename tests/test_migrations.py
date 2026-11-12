import sqlite3
from contextlib import closing

import pytest

from labdefender.local import LocalOperationError
from labdefender.migrations import _migrate, database_schema_state, migrate_database


def create_legacy_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER NOT NULL PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                full_name VARCHAR(256) NOT NULL,
                role VARCHAR(7) NOT NULL
            );
            CREATE TABLE study_groups (
                id INTEGER NOT NULL PRIMARY KEY,
                teacher_id INTEGER NOT NULL,
                name VARCHAR(128) NOT NULL,
                invite_token VARCHAR(32) NOT NULL
            );
            INSERT INTO users VALUES (1, 202, 'Студент', 'student'),
                (2, 101, 'Преподаватель', 'teacher');
            INSERT INTO study_groups VALUES (1, 2, 'ИВТ-101', 'token');
            CREATE TABLE assignments (
                id INTEGER NOT NULL PRIMARY KEY,
                group_id INTEGER NOT NULL,
                title VARCHAR(256) NOT NULL,
                description TEXT NOT NULL
            );
            CREATE TABLE submissions (
                id INTEGER NOT NULL PRIMARY KEY,
                assignment_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                attempt_number INTEGER NOT NULL,
                kind VARCHAR(4) NOT NULL,
                status VARCHAR(8) NOT NULL,
                content TEXT,
                file_id TEXT,
                review_comment TEXT,
                submitted_at DATETIME,
                approved_at DATETIME
            );
            CREATE TABLE defense_days (
                id INTEGER NOT NULL PRIMARY KEY,
                assignment_id INTEGER NOT NULL,
                date DATE NOT NULL,
                capacity INTEGER NOT NULL,
                UNIQUE (assignment_id, date)
            );
            CREATE TABLE queue_entries (
                id INTEGER NOT NULL PRIMARY KEY,
                defense_day_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL UNIQUE,
                order_at DATETIME NOT NULL
            );
            INSERT INTO assignments (id, group_id, title, description)
                VALUES (1, 1, 'ЛР 1', 'Условие');
            INSERT INTO submissions
                (id, assignment_id, student_id, attempt_number, kind, status, content,
                 submitted_at, approved_at)
                VALUES (1, 1, 1, 1, 'text', 'approved', 'Решение',
                        '2026-11-10 10:00:00', '2026-11-10 10:00:00');
            INSERT INTO defense_days (id, assignment_id, date, capacity)
                VALUES (1, 1, '2026-11-13', 3);
            INSERT INTO queue_entries (id, defense_day_id, submission_id, order_at)
                VALUES (1, 1, 1, '2026-11-10 10:00:00');
            """
        )


def test_legacy_database_is_migrated_with_backup(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    create_legacy_database(path)

    migrated, backup = migrate_database(path)

    assert migrated == path.resolve()
    assert backup is not None and backup.is_file()
    assert database_schema_state(path) == "new"
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "defense_slots" in tables
        assert "defense_days" not in tables
        assignment = connection.execute(
            "SELECT title, description, format FROM assignments WHERE id = 1"
        ).fetchone()
        assert assignment == ("ЛР 1", "Условие", "online")
        slot = connection.execute(
            "SELECT group_id, date, start_time, end_time, capacity, is_open "
            "FROM defense_slots WHERE id = 1"
        ).fetchone()
        assert slot[0] == 1
        assert slot[1] == "2026-11-13"
        assert slot[2].startswith("09:00:00")
        assert slot[3].startswith("18:00:00")
        assert slot[4:] == (3, 1)
        queue_columns = {row[1] for row in connection.execute('PRAGMA table_info("queue_entries")')}
        assert "slot_id" in queue_columns
        assert "defense_day_id" not in queue_columns
        entry = connection.execute(
            "SELECT slot_id, submission_id, student_id, assignment_id, order_at "
            "FROM queue_entries WHERE id = 1"
        ).fetchone()
        assert entry == (1, 1, 1, 1, "2026-11-10 10:00:00")


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    create_legacy_database(path)

    _, backup = migrate_database(path)
    assert backup is not None

    migrated_again, backup_again = migrate_database(path)
    assert migrated_again == path.resolve()
    assert backup_again is None


def create_assignment_slots_database(path):
    """The schema before group queues: every slot belongs to one assignment."""
    create_legacy_database(path)
    with closing(sqlite3.connect(path)) as connection:
        _migrate(connection)
        connection.executescript(
            """
            INSERT INTO assignments (id, group_id, title, description, format)
                VALUES (2, 1, 'ЛР 2', 'Условие', 'online');
            INSERT INTO submissions
                (id, assignment_id, student_id, attempt_number, kind, status, content,
                 submitted_at, approved_at)
                VALUES (2, 2, 1, 1, 'text', 'approved', 'Решение 2',
                        '2026-11-10 11:00:00', '2026-11-10 11:00:00');
            INSERT INTO defense_slots (id, assignment_id, date, start_time, end_time, capacity)
                VALUES (2, 2, '2026-11-13', '09:00:00.000000', '18:00:00.000000', 2);
            INSERT INTO queue_entries (id, slot_id, submission_id, order_at)
                VALUES (2, 2, 2, '2026-11-10 11:00:00');
            """
        )
        connection.commit()


def test_assignment_slots_become_group_slots_and_entries_name_the_work(tmp_path):
    path = tmp_path / "slots.sqlite3"
    create_assignment_slots_database(path)
    assert database_schema_state(path) == "slots"

    _, backup = migrate_database(path)

    assert backup is not None and "before-migrate" in backup.name
    assert database_schema_state(path) == "new"
    with closing(sqlite3.connect(path)) as connection:
        # Two slots of two assignments at the same time stay separate slots of the group.
        assert connection.execute(
            "SELECT id, group_id, capacity FROM defense_slots ORDER BY id"
        ).fetchall() == [(1, 1, 3), (2, 1, 2)]
        assert connection.execute(
            "SELECT id, slot_id, student_id, assignment_id FROM queue_entries ORDER BY id"
        ).fetchall() == [(1, 1, 1, 1), (2, 2, 1, 2)]
        slot_columns = {row[1] for row in connection.execute('PRAGMA table_info("defense_slots")')}
        assert "assignment_id" not in slot_columns
        indexes = {row[1] for row in connection.execute('PRAGMA index_list("queue_entries")')}
        assert "ix_queue_entries_slot_id" in indexes


def test_failed_queue_migration_restores_the_database(tmp_path):
    path = tmp_path / "broken.sqlite3"
    create_assignment_slots_database(path)
    with closing(sqlite3.connect(path)) as connection:
        # Two bookings of one work cannot exist in the new schema.
        connection.executescript(
            """
            INSERT INTO submissions
                (id, assignment_id, student_id, attempt_number, kind, status, content,
                 submitted_at, approved_at)
                VALUES (3, 2, 1, 2, 'text', 'approved', 'Ещё', '2026-11-10 12:00:00',
                        '2026-11-10 12:00:00');
            INSERT INTO queue_entries (id, slot_id, submission_id, order_at)
                VALUES (3, 2, 3, '2026-11-10 12:00:00');
            """
        )
        connection.commit()
    with pytest.raises(LocalOperationError, match="восстановлена из копии"):
        migrate_database(path)
    assert database_schema_state(path) == "slots"


async def test_database_waiting_for_migration_can_be_backed_up(tmp_path):
    """A full database of the previous version: backup works before `tools migrate`."""
    from labdefender.db.session import create_database, initialize_database
    from labdefender.local import backup_database, check_database

    source = tmp_path / "previous.sqlite3"
    engine, _ = create_database(f"sqlite+aiosqlite:///{source}")
    await initialize_database(engine)
    await engine.dispose()
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(
            """
            DROP TABLE queue_entries;
            DROP TABLE defense_slots;
            CREATE TABLE defense_slots (
                id INTEGER NOT NULL PRIMARY KEY,
                assignment_id INTEGER NOT NULL REFERENCES assignments (id),
                date DATE NOT NULL,
                start_time TIME NOT NULL,
                end_time TIME NOT NULL,
                capacity INTEGER NOT NULL,
                is_open BOOLEAN NOT NULL DEFAULT 1
            );
            CREATE TABLE queue_entries (
                id INTEGER NOT NULL PRIMARY KEY,
                slot_id INTEGER NOT NULL REFERENCES defense_slots (id),
                submission_id INTEGER NOT NULL UNIQUE REFERENCES submissions (id),
                order_at DATETIME NOT NULL
            );
            """
        )
    assert database_schema_state(source) == "slots"
    with pytest.raises(LocalOperationError, match="устарела"):
        check_database(source)

    copy = backup_database(source, tmp_path / "copy.sqlite3")
    migrate_database(copy)
    assert database_schema_state(copy) == "new"
    assert check_database(copy) == ()


async def test_bot_refuses_to_start_on_a_schema_waiting_for_migration(tmp_path):
    from labdefender.app import run
    from labdefender.config import Settings

    path = tmp_path / "slots.sqlite3"
    create_assignment_slots_database(path)
    settings = Settings(
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        database_url=f"sqlite+aiosqlite:///{path}",
        _env_file=None,
    )
    with pytest.raises(LocalOperationError, match="tools migrate"):
        await run(settings)
    assert database_schema_state(path) == "slots"
