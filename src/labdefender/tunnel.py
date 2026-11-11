"""Public HTTPS address for the Mini App through Cloudflare's `cloudflared`.

Two kinds of tunnels, both started with `--tunnel`:

- quick tunnel: no account, a new `https://….trycloudflare.com` address on every start of
  cloudflared; buttons with an old address in earlier messages stop working. By default it lives
  and dies with the bot. `python -m labdefender.tools tunnel` keeps one in its own window
  instead (`shared_tunnel`): a bot started with `--tunnel` takes its address
  (`running_tunnel`), so restarting the bot keeps the address;
- permanent tunnel: created once in the Cloudflare dashboard for a hostname on your own domain.
  `.env` keeps its token (TUNNEL_TOKEN) and that address (WEBAPP_URL). A backup laptop runs
  the same tunnel with the same token, so every button keeps working after a switch-over.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout

from labdefender.local import LocalOperationError

TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# cloudflared logs this once a permanent tunnel can receive requests.
CONNECTED = "Registered tunnel connection"
START_TIMEOUT = 30.0
STOP_TIMEOUT = 10.0
ERROR_LIMIT = 300
# Next to the instance locks: per local user, shared by checkouts. Holds no secrets.
STATE_DIR = Path(tempfile.gettempdir()) / "labdefender-locks"
INSTALL_HINT = (
    "Не найден cloudflared. Установите его: macOS — `brew install cloudflared`, "
    "Windows — `winget install --id Cloudflare.cloudflared`; затем откройте новый терминал."
)


@dataclass(frozen=True)
class Tunnel:
    url: str
    process: subprocess.Popen


def find_tunnel_url(line: str) -> str | None:
    match = TUNNEL_URL.search(line)
    return match.group(0) if match else None


def is_connected(line: str) -> bool:
    return CONNECTED in line


def local_web_url(host: str, port: int) -> str:
    # A server listening on all interfaces is still reached locally through the loopback.
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _cloudflared(*arguments: str) -> list[str]:
    executable = shutil.which("cloudflared")
    if executable is None:
        raise LocalOperationError(INSTALL_HINT)
    return [executable, "tunnel", "--no-autoupdate", *arguments]


@contextmanager
def _run_until(
    argv: Sequence[str],
    *,
    ready: Callable[[str], str | None],
    timeout: float,
    failure: str,
    env: Mapping[str, str] | None = None,
) -> Iterator[tuple[str, subprocess.Popen]]:
    """Start cloudflared, wait for the first line `ready` accepts, stop the process on exit."""
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env) if env is not None else None,
        )
    except OSError:
        raise LocalOperationError(INSTALL_HINT) from None
    found: queue.Queue[str | None] = queue.Queue()
    # The cause for the operator: the last ERR line, or what cloudflared said before exiting
    # (a bad token is reported without the ERR prefix). cloudflared never prints the token.
    last_error: list[str] = []
    last_said: list[str] = []

    def drain() -> None:
        # Keep reading until cloudflared exits: a full pipe would block it.
        reported = False
        for line in process.stdout:
            text = line.strip()
            if " ERR " in line:
                last_error[:] = [text[-ERROR_LIMIT:]]
            elif text and not text.startswith("See '"):
                last_said[:] = [text[-ERROR_LIMIT:]]
            value = None if reported else ready(line)
            if value:
                found.put(value)
                reported = True
        if not reported:
            found.put(None)

    threading.Thread(target=drain, name="cloudflared-output", daemon=True).start()
    try:
        exited = False
        try:
            value = found.get(timeout=timeout)
            exited = value is None
        except queue.Empty:
            value = None
        if value is None:
            cause = last_error or (last_said if exited else [])
            raise LocalOperationError(failure + (f"\ncloudflared: {cause[0]}" if cause else ""))
        yield value, process
    finally:
        process.terminate()
        try:
            process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def quick_tunnel(
    local_url: str, *, argv: Sequence[str] | None = None, timeout: float = START_TIMEOUT
) -> Iterator[Tunnel]:
    """A temporary trycloudflare.com address that leads to `local_url`."""
    with _run_until(
        argv or _cloudflared("--url", local_url),
        ready=find_tunnel_url,
        timeout=timeout,
        failure=(
            "cloudflared не выдал адрес временного туннеля. Проверьте интернет; "
            "временный туннель не запускается, если есть ~/.cloudflared/config.yml."
        ),
    ) as (url, process):
        yield Tunnel(url=url, process=process)


@contextmanager
def permanent_tunnel(
    token: str,
    url: str,
    *,
    argv: Sequence[str] | None = None,
    timeout: float = START_TIMEOUT,
) -> Iterator[Tunnel]:
    """The tunnel with a permanent address `url`, configured in the Cloudflare dashboard.

    The token goes through the environment, not the command line, where other local users
    could read it in the process list.
    """
    with _run_until(
        argv or _cloudflared("run"),
        ready=lambda line: "connected" if is_connected(line) else None,
        timeout=timeout,
        failure=(
            "cloudflared не подключил постоянный туннель. Проверьте интернет и TUNNEL_TOKEN: "
            "это токен туннеля из панели Cloudflare (Zero Trust → Networks → Tunnels)."
        ),
        env={**os.environ, "TUNNEL_TOKEN": token},
    ) as (_, process):
        yield Tunnel(url=url, process=process)


def _shared_files(local_url: str, state_dir: Path | None) -> tuple[Path, FileLock]:
    directory = state_dir or STATE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9]+", "-", local_url).strip("-")
    return directory / f"tunnel-{name}.json", FileLock(str(directory / f"tunnel-{name}.lock"))


def _read_address(state: Path, local_url: str) -> str | None:
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not TUNNEL_URL.fullmatch(url):
        return None
    return url if data.get("local_url") == local_url else None


def running_tunnel(local_url: str, *, state_dir: Path | None = None) -> str | None:
    """Address of the quick tunnel `shared_tunnel` keeps for `local_url`, if it is running.

    The keeper holds a file lock for its whole life; the operating system releases it when
    the process ends, so a stale address file is never taken for a live tunnel.
    """
    state, lock = _shared_files(local_url, state_dir)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return _read_address(state, local_url)
    lock.release()
    return None


@contextmanager
def shared_tunnel(
    local_url: str,
    *,
    argv: Sequence[str] | None = None,
    timeout: float = START_TIMEOUT,
    state_dir: Path | None = None,
) -> Iterator[Tunnel]:
    """Keep a quick tunnel to `local_url` and publish its address for bots on this computer."""
    state, lock = _shared_files(local_url, state_dir)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        current = _read_address(state, local_url)
        raise LocalOperationError(
            "Отдельный туннель к этому компьютеру уже запущен"
            + (f": {current}." if current else ".")
            + " Закройте его окно, если нужен новый адрес."
        ) from None
    try:
        with quick_tunnel(local_url, argv=argv, timeout=timeout) as tunnel:
            state.write_text(
                json.dumps({"url": tunnel.url, "local_url": local_url}), encoding="utf-8"
            )
            try:
                yield tunnel
            finally:
                state.unlink(missing_ok=True)
    finally:
        lock.release()
