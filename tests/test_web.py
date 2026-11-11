import asyncio
import hashlib
import hmac
import json
import re
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import aiohttp
import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SetChatMenuButton
from aiogram.types import MenuButtonDefault, MenuButtonWebApp
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from test_queue import message_update

import labdefender.__main__ as entry
from labdefender import tools
from labdefender.app import create_dispatcher
from labdefender.config import Settings
from labdefender.db.models import User
from labdefender.enums import Role
from labdefender.handlers.notifications import Notifier
from labdefender.local import LocalOperationError
from labdefender.services.users import ensure_user
from labdefender.tunnel import (
    Tunnel,
    find_tunnel_url,
    is_connected,
    local_web_url,
    permanent_tunnel,
    quick_tunnel,
    running_tunnel,
    shared_tunnel,
)
from labdefender.web.app import STATIC, configure_menu_button, create_web_app, web_server
from labdefender.web.reachability import address_check, check_public_address

TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
TUNNEL = "https://quiet-lake-1234.trycloudflare.com"
PERMANENT = "https://lab.example.org"
IMPORT = re.compile(r'import\s*(?:\{([^}]*)\}|\*\s+as\s+\w+)\s*from\s*"(\.[^"]+)"')


def init_data(*, user_id=101, token=TOKEN, auth_date=None, user=True, name=None, **fields):
    values = {
        "auth_date": str(int((auth_date or datetime.now(UTC)).timestamp())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        **fields,
    }
    if user:
        person = (
            {"id": user_id, "first_name": name}
            if name
            else {"id": user_id, "first_name": "Иван", "last_name": "Петров", "username": "ivan"}
        )
        values["user"] = json.dumps(person, ensure_ascii=False, separators=(",", ":"))
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def settings(**kwargs):
    return Settings(bot_token=TOKEN, _env_file=None, **kwargs)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
async def client(database, bot):
    _, sessions = database
    app = create_web_app(
        sessions,
        bot_token=SecretStr(TOKEN),
        auth_max_age=3600,
        bot=bot,
        notifier=Notifier(),
        storage=MemoryStorage(),
    )
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def test_signed_init_data_opens_profile_and_registers_student(database, client):
    response = await client.get("/api/me", headers={"Authorization": f"tma {init_data()}"})
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.json() == {
        "telegram_id": 101,
        "name": "Иван Петров",
        "telegram_name": "Иван Петров",
        "custom_name": False,
        "role": "student",
        "role_label": "студент",
        "admin": False,
        "can_register": False,
    }
    _, sessions = database
    async with sessions() as session:
        user = await session.scalar(select(User))
        assert (user.telegram_id, user.username, user.role) == (101, "ivan", Role.STUDENT)


async def test_profile_keeps_persisted_teacher_role(database, client):
    _, sessions = database
    async with sessions() as session:
        user = await ensure_user(session, telegram_id=101, username=None, full_name="Old")
        user.role = Role.TEACHER
        await session.commit()
    response = await client.get("/api/me", headers={"Authorization": f"tma {init_data()}"})
    body = await response.json()
    assert (body["role"], body["role_label"], body["name"]) == (
        "teacher",
        "преподаватель",
        "Иван Петров",
    )


def tampered():
    return init_data().replace("%22id%22%3A101", "%22id%22%3A202")


@pytest.mark.parametrize(
    "header,message",
    [
        (None, "Откройте приложение из Telegram."),
        (f"Bearer {init_data()}", "Откройте приложение из Telegram."),
        ("tma ", "Откройте приложение из Telegram."),
        (f"tma {tampered()}", "Telegram не подтвердил вход"),
        (f"tma {init_data(token='654321:' + 'B' * 35)}", "Telegram не подтвердил вход"),
        ("tma not-init-data", "Telegram не подтвердил вход"),
        (f"tma {init_data(user=False)}", "из личного чата"),
        (
            f"tma {init_data(auth_date=datetime.now(UTC) - timedelta(hours=2))}",
            "Сеанс устарел",
        ),
        (
            f"tma {init_data(auth_date=datetime.now(UTC) + timedelta(hours=1))}",
            "Сеанс устарел",
        ),
    ],
)
async def test_unsigned_foreign_or_stale_requests_are_rejected(database, client, header, message):
    headers = {} if header is None else {"Authorization": header}
    response = await client.get("/api/me", headers=headers)
    assert response.status == 401
    assert message in (await response.json())["error"]
    _, sessions = database
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0


async def test_tampered_payload_really_changes_the_user_id():
    assert "%22id%22%3A202" in tampered()


async def test_page_and_assets_are_served_with_safe_headers(client):
    page = await client.get("/app/")
    assert page.status == 200
    html = await page.text()
    assert 'src="https://telegram.org/js/telegram-web-app.js"' in html
    assert '<script type="module" src="app.js">' in html and 'href="style.css"' in html
    assert page.headers["Cache-Control"] == "no-cache"
    assert "script-src 'self' https://telegram.org" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    for script in sorted(STATIC.rglob("*.js")):
        response = await client.get(f"/app/{script.relative_to(STATIC).as_posix()}")
        assert response.status == 200
        # Module scripts are refused by browsers unless served as JavaScript.
        assert response.headers["Content-Type"] == "text/javascript; charset=utf-8"
    api_client = await client.get("/app/lib/api.js")
    assert "Authorization" in await api_client.text()
    style = await client.get("/app/style.css")
    assert style.headers["Content-Type"] == "text/css; charset=utf-8"
    assert (await client.get("/app/missing.js")).status == 404


def test_page_modules_import_existing_exports():
    # No JS toolchain in the project: catch broken relative imports and misspelled names.
    scripts = sorted(STATIC.rglob("*.js"))
    assert {script.relative_to(STATIC).as_posix() for script in scripts} >= {
        "app.js",
        "lib/api.js",
        "lib/dom.js",
        "lib/router.js",
        "lib/telegram.js",
        "views/attempt.js",
        "views/common.js",
        "views/home.js",
        "views/new-slot.js",
        "views/profile.js",
        "views/slots.js",
        "views/student.js",
        "views/teacher.js",
    }
    for script in scripts:
        for names, target in IMPORT.findall(script.read_text(encoding="utf-8")):
            module = (script.parent / target).resolve()
            assert module.is_file(), (script.name, target)
            source = module.read_text(encoding="utf-8")
            for name in filter(None, (part.strip() for part in names.split(","))):
                pattern = rf"export (?:async )?(?:function|class|const|let) {name}\b"
                assert re.search(pattern, source), (script.name, target, name)


@pytest.mark.parametrize("webapp_url", [TUNNEL, ""])
async def test_app_command_and_help_button_carry_current_address(database, bot, webapp_url):
    _, sessions = database
    dispatcher = create_dispatcher(sessions, settings=settings(webapp_url=webapp_url))
    await dispatcher.feed_update(bot, message_update("/app"))
    answer = bot.session.calls[-1]
    await dispatcher.feed_update(bot, message_update("/help"))
    help_message = bot.session.calls[-1]
    first_help_button = help_message.reply_markup.inline_keyboard[0][0]
    if webapp_url:
        [[button]] = answer.reply_markup.inline_keyboard
        assert (button.text, button.web_app.url) == ("📱 Открыть приложение", f"{TUNNEL}/app/")
        assert first_help_button.web_app.url == f"{TUNNEL}/app/"
        assert "/app — открыть приложение" in help_message.text
    else:
        assert answer.text.startswith("Приложение сейчас выключено")
        assert answer.reply_markup is None
        assert first_help_button.web_app is None
        assert "/app" not in help_message.text


def test_webapp_url_must_be_plain_https():
    assert settings().webapp_url == ""
    assert settings(webapp_url=f" {TUNNEL}/ ").webapp_url == TUNNEL
    for value in ("http://example.com", f"{TUNNEL}?a=1", f"{TUNNEL}#x", "https://"):
        with pytest.raises(ValidationError):
            settings(webapp_url=value)
    with pytest.raises(ValidationError):
        settings(web_port=0)


async def test_menu_button_opens_mini_app_or_returns_to_commands(bot):
    await configure_menu_button(bot, TUNNEL)
    call = bot.session.calls[-1]
    assert isinstance(call, SetChatMenuButton) and call.chat_id is None
    assert isinstance(call.menu_button, MenuButtonWebApp)
    assert call.menu_button.web_app.url == f"{TUNNEL}/app/"
    await configure_menu_button(bot, "")
    assert isinstance(bot.session.calls[-1].menu_button, MenuButtonDefault)


async def test_web_server_runs_with_bot_and_stops(database, bot):
    _, sessions = database
    port = free_port()
    config = settings(webapp_url=TUNNEL, web_port=port)
    async with aiohttp.ClientSession() as http:
        async with web_server(
            sessions, config, bot=bot, notifier=Notifier(), storage=MemoryStorage()
        ):
            async with http.get(f"http://127.0.0.1:{port}/app/") as response:
                assert response.status == 200
        with pytest.raises(aiohttp.ClientConnectionError):
            await http.get(f"http://127.0.0.1:{port}/app/")


async def test_web_server_is_off_without_url_and_reports_busy_port(database, bot):
    _, sessions = database
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        async with web_server(
            sessions, settings(web_port=port), bot=bot, notifier=Notifier(), storage=MemoryStorage()
        ):
            pass
        with pytest.raises(LocalOperationError, match=f"127.0.0.1:{port}"):
            async with web_server(
                sessions,
                settings(webapp_url=TUNNEL, web_port=port),
                bot=bot,
                notifier=Notifier(),
                storage=MemoryStorage(),
            ):
                pass


def test_tunnel_address_and_local_target():
    line = f"2026-11-11T10:00:00Z INF |  {TUNNEL}                                |"
    assert find_tunnel_url(line) == TUNNEL
    assert find_tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None
    assert local_web_url("127.0.0.1", 8080) == "http://127.0.0.1:8080"
    assert local_web_url("0.0.0.0", 8080) == "http://127.0.0.1:8080"
    assert local_web_url("::1", 9000) == "http://[::1]:9000"


def child(code):
    return [sys.executable, "-c", code]


def test_quick_tunnel_yields_address_and_stops_cloudflared():
    script = (
        "import time\n"
        "print('INF Requesting new quick Tunnel', flush=True)\n"
        f"print('INF |  {TUNNEL}  |', flush=True)\n"
        "for _ in range(1000): print('INF noise', flush=True)\n"
        "time.sleep(60)\n"
    )
    with quick_tunnel("http://127.0.0.1:8080", argv=child(script), timeout=10) as tunnel:
        assert tunnel.url == TUNNEL
        assert tunnel.process.poll() is None
    assert tunnel.process.poll() is not None


@pytest.mark.parametrize(
    "script,timeout",
    [
        ("print('ERR failed to request quick Tunnel', flush=True)", 10),
        ("import time; time.sleep(60)", 0.5),
    ],
)
def test_quick_tunnel_without_address_fails_clearly(script, timeout):
    with pytest.raises(LocalOperationError, match="не выдал адрес"):
        with quick_tunnel("http://127.0.0.1:8080", argv=child(script), timeout=timeout):
            pass


def test_missing_cloudflared_explains_installation(monkeypatch):
    monkeypatch.setattr("labdefender.tunnel.shutil.which", lambda name: None)
    with pytest.raises(LocalOperationError, match="brew install cloudflared"):
        with quick_tunnel("http://127.0.0.1:8080"):
            pass


@pytest.mark.parametrize("argv,expected", [(["--tunnel"], TUNNEL), ([], "")])
def test_tunnel_flag_passes_temporary_address_to_bot(monkeypatch, tmp_path, argv, expected):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    started, targets = [], []

    @contextmanager
    def fake_tunnel(local_url):
        targets.append(local_url)
        yield Tunnel(url=TUNNEL, process=None)

    async def fake_run(config):
        started.append(config)

    monkeypatch.setattr(entry, "quick_tunnel", fake_tunnel)
    monkeypatch.setattr(entry, "running_tunnel", lambda local_url: None)
    monkeypatch.setattr(entry, "run", fake_run)
    entry.main(argv)
    [config] = started
    assert config.webapp_url == expected
    assert targets == (["http://127.0.0.1:8080"] if argv else [])


def test_tunnel_flag_takes_the_address_of_a_tunnel_kept_in_another_window(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    started, asked = [], []

    def kept(local_url):
        asked.append(local_url)
        return TUNNEL

    def unexpected(local_url):
        raise AssertionError("A kept tunnel must be reused, not replaced")

    async def fake_run(config):
        started.append(config)

    monkeypatch.setattr(entry, "running_tunnel", kept)
    monkeypatch.setattr(entry, "quick_tunnel", unexpected)
    monkeypatch.setattr(entry, "run", fake_run)
    entry.main(["--tunnel"])
    assert asked == ["http://127.0.0.1:8080"]
    assert [config.webapp_url for config in started] == [TUNNEL]
    assert "перезапуск бота его не меняет" in capsys.readouterr().out


def announcing(url=TUNNEL):
    return child(f"import time\nprint('INF |  {url}  |', flush=True)\ntime.sleep(60)\n")


def test_kept_tunnel_publishes_its_address_only_while_it_runs(tmp_path):
    local = "http://127.0.0.1:8080"
    assert running_tunnel(local, state_dir=tmp_path) is None
    with shared_tunnel(local, argv=announcing(), timeout=10, state_dir=tmp_path) as tunnel:
        assert tunnel.url == TUNNEL
        assert running_tunnel(local, state_dir=tmp_path) == TUNNEL
        # Another local server (WEB_PORT) needs its own tunnel.
        assert running_tunnel("http://127.0.0.1:9090", state_dir=tmp_path) is None
        with pytest.raises(LocalOperationError, match=f"уже запущен: {TUNNEL}"):
            with shared_tunnel(local, argv=announcing(), timeout=10, state_dir=tmp_path):
                pass
    assert tunnel.process.poll() is not None
    assert running_tunnel(local, state_dir=tmp_path) is None
    assert not list(tmp_path.glob("*.json"))


def test_address_file_without_a_live_keeper_is_ignored(tmp_path):
    local = "http://127.0.0.1:8080"
    # A keeper that was killed leaves its file, but the operating system frees its lock.
    (tmp_path / "tunnel-http-127-0-0-1-8080.json").write_text(
        json.dumps({"url": TUNNEL, "local_url": local}), encoding="utf-8"
    )
    assert running_tunnel(local, state_dir=tmp_path) is None


def test_tunnel_window_reports_a_stopped_cloudflared(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tools, "Settings", lambda: settings())

    @contextmanager
    def ending(local_url):
        process = subprocess.Popen(child("pass"))
        yield Tunnel(url=TUNNEL, process=process)

    monkeypatch.setattr(tools, "shared_tunnel", ending)
    with pytest.raises(LocalOperationError, match="cloudflared остановился"):
        tools.keep_tunnel()
    assert f"Mini App: {TUNNEL}/app/" in capsys.readouterr().out
    monkeypatch.setattr(tools, "Settings", lambda: settings(tunnel_token="token-value"))
    with pytest.raises(LocalOperationError, match="TUNNEL_TOKEN задан"):
        tools.keep_tunnel()


def permanent_settings(monkeypatch, tmp_path, **env):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_tunnel_token_is_one_plain_line():
    assert settings(tunnel_token=" abc.def-123 ").tunnel_token.get_secret_value() == "abc.def-123"
    with pytest.raises(ValidationError) as error:
        settings(tunnel_token="abc def")
    assert "abc def" not in str(error.value)


def test_permanent_tunnel_passes_token_privately_and_waits_for_connection():
    script = (
        "import os, sys, time\n"
        "assert os.environ['TUNNEL_TOKEN'] == 'token-value'\n"
        "assert 'token-value' not in ' '.join(sys.argv)\n"
        "print('INF Starting tunnel', flush=True)\n"
        "print('INF Registered tunnel connection connIndex=0 location=ams01', flush=True)\n"
        "time.sleep(60)\n"
    )
    with permanent_tunnel("token-value", PERMANENT, argv=child(script), timeout=10) as tunnel:
        assert tunnel.url == PERMANENT
        assert tunnel.process.poll() is None
    assert tunnel.process.poll() is not None
    assert is_connected("INF Registered tunnel connection connIndex=1")
    assert not is_connected("INF Starting tunnel")


@pytest.mark.parametrize(
    "output,cause",
    [
        # What cloudflared 2026.10 prints for a malformed token, then it exits.
        (
            ["Provided Tunnel token is not valid.", "See 'cloudflared tunnel run --help'."],
            "Provided Tunnel token is not valid.",
        ),
        (
            ["2026-11-11T10:00:00Z ERR Register tunnel error", "INF Retrying"],
            "2026-11-11T10:00:00Z ERR Register tunnel error",
        ),
    ],
)
def test_permanent_tunnel_failure_shows_what_cloudflared_said(output, cause):
    script = "".join(f"print({line!r}, flush=True)\n" for line in output)
    with pytest.raises(LocalOperationError) as error:
        with permanent_tunnel("bad", PERMANENT, argv=child(script), timeout=10):
            pass
    message = str(error.value)
    assert "TUNNEL_TOKEN" in message
    assert message.endswith(f"cloudflared: {cause}")


def test_tunnel_flag_uses_permanent_tunnel_when_token_is_set(monkeypatch, tmp_path):
    permanent_settings(monkeypatch, tmp_path, TUNNEL_TOKEN="token-value", WEBAPP_URL=PERMANENT)
    started, opened = [], []

    @contextmanager
    def fake_permanent(token, url):
        opened.append((token, url))
        yield Tunnel(url=url, process=None)

    def unexpected(local_url):
        raise AssertionError("The quick tunnel must not start with TUNNEL_TOKEN")

    async def fake_run(config):
        started.append(config)

    monkeypatch.setattr(entry, "permanent_tunnel", fake_permanent)
    monkeypatch.setattr(entry, "quick_tunnel", unexpected)
    monkeypatch.setattr(entry, "run", fake_run)
    entry.main(["--tunnel"])
    assert opened == [("token-value", PERMANENT)]
    [config] = started
    assert config.webapp_url == PERMANENT


def test_permanent_tunnel_needs_its_address(monkeypatch, tmp_path):
    permanent_settings(monkeypatch, tmp_path, TUNNEL_TOKEN="token-value")
    monkeypatch.setattr(entry, "run", None)
    with pytest.raises(SystemExit, match="WEBAPP_URL"):
        entry.main(["--tunnel"])


def test_doctor_explains_tunnel_settings(capsys, monkeypatch):
    monkeypatch.setattr("labdefender.tools.shutil.which", lambda name: None)
    tools.mini_app_doctor(settings(webapp_url=PERMANENT, tunnel_token="token-value"))
    output = capsys.readouterr().out
    assert "постоянный туннель" in output and "http://127.0.0.1:8080" in output
    assert "cloudflared не установлен" in output
    assert "token-value" not in output
    tools.mini_app_doctor(settings(webapp_url=TUNNEL))
    assert "временный адрес trycloudflare.com" in capsys.readouterr().out
    with pytest.raises(LocalOperationError, match="WEBAPP_URL пуст"):
        tools.mini_app_doctor(settings(tunnel_token="token-value"))


async def test_health_names_this_process_without_sign_in(database, bot):
    _, sessions = database
    app = create_web_app(
        sessions,
        bot_token=SecretStr(TOKEN),
        auth_max_age=3600,
        bot=bot,
        notifier=Notifier(),
        storage=MemoryStorage(),
        instance="this-process",
    )
    async with TestClient(TestServer(app)) as test_client:
        response = await test_client.get("/health")
        assert response.status == 200
        assert await response.json() == {"app": "labdefender", "instance": "this-process"}
        url = str(test_client.make_url("/health"))
        assert await check_public_address(url, "this-process", attempts=1)


async def test_address_check_warns_about_other_process_and_dead_address(database, bot, caplog):
    _, sessions = database
    app = create_web_app(
        sessions,
        bot_token=SecretStr(TOKEN),
        auth_max_age=3600,
        bot=bot,
        notifier=Notifier(),
        storage=MemoryStorage(),
        instance="backup-laptop",
    )
    async with TestClient(TestServer(app)) as test_client:
        url = str(test_client.make_url("/health"))
        assert not await check_public_address(url, "this-process", attempts=3, delay=0)
        assert "другой запуск LabDefender" in caplog.text
        missing = str(test_client.make_url("/nothing"))
        assert not await check_public_address(missing, "this-process", attempts=2, delay=0)
        assert "ответ HTTP 404" in caplog.text
    closed = f"http://127.0.0.1:{free_port()}/health"
    assert not await check_public_address(closed, "this-process", attempts=1)
    assert "ошибка соединения" in caplog.text


async def test_address_check_runs_beside_the_bot_and_stops_with_it(monkeypatch):
    calls = []

    async def endless(url, instance):
        calls.append((url, instance))
        await asyncio.Event().wait()

    monkeypatch.setattr("labdefender.web.reachability.check_public_address", endless)
    async with address_check("", None):
        pass
    async with address_check(PERMANENT, "this-process"):
        await asyncio.sleep(0)
    assert calls == [(f"{PERMANENT}/health", "this-process")]
