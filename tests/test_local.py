import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest
from aiogram.methods import GetMe, GetWebhookInfo
from aiogram.types import WebhookInfo

from labdefender import tools
from labdefender.config import DatabaseSettings, Settings
from labdefender.demo import seed_demo
from labdefender.local import (
    LocalOperationError,
    backup_database,
    check_database,
    database_lock,
    database_path,
    instance_locks,
    restore_database,
)

TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def child(code, *args):
    return subprocess.run(
        [sys.executable, "-c", code, *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def test_database_settings_do_not_require_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert (
        database_path(DatabaseSettings().database_url) == tmp_path / "data" / "labdefender.sqlite3"
    )
    with pytest.raises(LocalOperationError):
        database_path("sqlite+aiosqlite:///:memory:")
    with pytest.raises(LocalOperationError):
        database_path("sqlite+aiosqlite:///test.sqlite3?uri=true")


def test_process_locks_block_same_database_and_same_bot_across_checkouts(tmp_path):
    source, other = tmp_path / "one.sqlite3", tmp_path / "two.sqlite3"
    code = (
        "from pathlib import Path; import sys; from labdefender.local import instance_locks; "
        "lock=instance_locks(Path(sys.argv[1]), bot_id=int(sys.argv[2])); "
        "lock.__enter__(); print('acquired'); lock.__exit__(None,None,None)"
    )
    with instance_locks(source, bot_id=77665544):
        same_database = child(code, source, 88776655)
        same_bot = child(code, other, 77665544)
        assert same_database.returncode != 0
        assert same_bot.returncode != 0
        assert "LocalOperationError" in same_database.stderr
        assert "LocalOperationError" in same_bot.stderr
    assert child(code, source, 77665544).returncode == 0


async def test_backup_includes_committed_wal_and_restore_never_overwrites(database, tmp_path):
    engine, _ = database
    source = database_path(str(engine.url))
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute(
            "INSERT INTO users (telegram_id, full_name, role) VALUES (101, 'Student', 'student')"
        )
        connection.commit()
        assert source.with_name(source.name + "-wal").exists()
        backup = backup_database(source, tmp_path / "backup.sqlite3")
    restored = restore_database(backup, tmp_path / "restored.sqlite3")
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("SELECT telegram_id FROM users").fetchall() == [(101,)]
    before = restored.read_bytes()
    with pytest.raises(LocalOperationError):
        restore_database(backup, restored)
    assert restored.read_bytes() == before
    with pytest.raises(LocalOperationError):
        backup_database(source, source)
    assert check_database(source) == ()


async def test_restore_respects_running_database_lock(database, tmp_path):
    engine, _ = database
    source = database_path(str(engine.url))
    target = tmp_path / "locked.sqlite3"
    code = (
        "import sys; from pathlib import Path; from labdefender.local import restore_database; "
        "restore_database(Path(sys.argv[1]),Path(sys.argv[2]))"
    )
    with database_lock(target):
        result = child(code, source, target)
        assert result.returncode != 0
        assert "LocalOperationError" in result.stderr
    assert not target.exists()


def test_invalid_backup_source_and_schema_leave_destination_untouched(tmp_path):
    corrupt, target = tmp_path / "corrupt.sqlite3", tmp_path / "output.sqlite3"
    corrupt.write_bytes(b"not a database")
    with pytest.raises(LocalOperationError):
        backup_database(corrupt, target)
    assert not target.exists()
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(LocalOperationError):
        backup_database(missing, target)
    assert not missing.exists()
    outdated = tmp_path / "outdated.sqlite3"
    with closing(sqlite3.connect(outdated)) as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    with pytest.raises(LocalOperationError, match="users"):
        check_database(outdated, allow_missing_tables=True)


async def test_demo_has_all_statuses_and_queue_and_cannot_replace_existing_file(tmp_path):
    target = tmp_path / "demo.sqlite3"
    data = await seed_demo(target, teacher_id=101, student_ids=(202, 203))
    assert check_database(target) == ()
    with closing(sqlite3.connect(target)) as connection:
        assert set(
            row[0] for row in connection.execute("SELECT DISTINCT status FROM submissions")
        ) == {"pending", "returned", "approved"}
        assert connection.execute("SELECT COUNT(*) FROM queue_entries").fetchone()[0] == 2
        assert (
            connection.execute("SELECT role FROM users WHERE telegram_id=101").fetchone()[0]
            == "teacher"
        )
    before = target.read_bytes()
    with pytest.raises(LocalOperationError):
        await seed_demo(target, teacher_id=999, student_ids=(888,))
    assert target.read_bytes() == before
    assert len(data["assignment_ids"]) == 3


async def test_failed_demo_removes_only_its_new_partial_database(tmp_path, monkeypatch):
    async def failure(*args, **kwargs):
        raise RuntimeError("Example seed failure")

    monkeypatch.setattr("labdefender.demo.create_group", failure)
    existing = tmp_path / "existing.txt"
    existing.write_text("keep", encoding="utf-8")
    new = tmp_path / "partial.sqlite3"
    with pytest.raises(RuntimeError):
        await seed_demo(new, teacher_id=101, student_ids=(202,))
    assert not new.exists()
    assert existing.read_text(encoding="utf-8") == "keep"


def test_doctor_does_not_print_secrets_or_contact_telegram(tmp_path, monkeypatch, capsys):
    configured = Settings(
        bot_token=TOKEN,
        teacher_registration_code="private-code",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'new.sqlite3'}",
        _env_file=None,
    )
    monkeypatch.setattr(tools, "Settings", lambda: configured)

    async def unexpected(*args):
        raise AssertionError("Network check must be opt-in")

    monkeypatch.setattr(tools, "telegram_check", unexpected)
    tools.doctor()
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert "private-code" not in output
    assert "база ещё не создана" in output
    assert not (tmp_path / "new.sqlite3").exists()


async def test_online_doctor_uses_read_only_telegram_methods(bot, monkeypatch, capsys):
    monkeypatch.setattr(tools, "Bot", lambda **kwargs: bot)
    original = bot.session.make_request

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, GetWebhookInfo):
            bot.session.calls.append(method)
            return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        return await original(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", request)
    await tools.telegram_check(Settings(bot_token=TOKEN, _env_file=None))
    assert all(isinstance(call, (GetMe, GetWebhookInfo)) for call in bot.session.calls)
    assert TOKEN not in capsys.readouterr().out


def test_offline_smoke_cli_does_not_require_environment_or_network(tmp_path):
    code = (
        "import os; os.environ.pop('BOT_TOKEN',None); from labdefender.tools import main; "
        "import sys; sys.argv=['tools','smoke']; main()"
    )
    result = child(code)
    assert result.returncode == 0, result.stderr
    assert "Без Telegram" in result.stdout


async def test_backup_accepts_database_created_before_additive_tables(tmp_path):
    source = tmp_path / "old.sqlite3"
    await seed_demo(source, teacher_id=101, student_ids=(202,))
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("DROP TABLE notification_settings")
        connection.commit()
    assert check_database(source) == ("notification_settings",)
    backup = backup_database(source, tmp_path / "backup.sqlite3")
    assert check_database(backup) == ("notification_settings",)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("DROP TABLE queue_entries")
        connection.commit()
    with pytest.raises(LocalOperationError, match="отсутствуют таблицы"):
        backup_database(source, tmp_path / "broken.sqlite3")
