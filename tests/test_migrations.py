import sqlite3

from labdefender.migrations import database_schema_state, migrate_database


def create_legacy_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
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
            "SELECT assignment_id, date, start_time, end_time, capacity, is_open "
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
            "SELECT slot_id, submission_id, order_at FROM queue_entries WHERE id = 1"
        ).fetchone()
        assert entry == (1, 1, "2026-11-10 10:00:00")


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    create_legacy_database(path)

    _, backup = migrate_database(path)
    assert backup is not None

    migrated_again, backup_again = migrate_database(path)
    assert migrated_again == path.resolve()
    assert backup_again is None
