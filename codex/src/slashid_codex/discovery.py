"""How the hook client finds, authenticates and starts the daemon (spec
"Discovery"). Standard library and ``platformdirs`` only: the client imports
nothing else, so plain dataclasses and ``json`` stand in for pydantic here."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import hmac
import http.client
import importlib.metadata
import json
import os
import secrets
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any

import platformdirs

APP_NAME = "slashid-ai-forwarder-codex"
APP_AUTHOR = "slashid"
INFO_FILE = "daemon.json"
LOCK_FILE = "daemon.lock"
LOG_FILE = "daemon.log"
# The daemon's stdout and stderr: crash tracebacks only. Kept apart from the
# rotated log, which Windows could not rename while a handle held it open.
STDERR_FILE = "daemon.stderr"
SPAWN_FAILED_FILE = "spawn-failed"
SPAWN_BACKOFF_S = 300.0
LOCK_POLL_S = 0.05
_MAX_INFO_BYTES = 4096
_MAX_PING_BYTES = 256

# Windows creation flags, spelled out: ``subprocess`` defines them only there.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def state_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override
    return Path(platformdirs.user_data_dir(APP_NAME, APP_AUTHOR))


def ensure_state_dir(path: Path) -> None:
    """``0700`` on POSIX; Windows keeps ``%LOCALAPPDATA%``'s inherited ACL."""
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        return
    if sys.platform != "win32":
        os.chmod(path, 0o700)


@functools.cache
def package_version() -> str:
    try:
        return importlib.metadata.version("slashid-codex")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


# --------------------------------------------------------------------------
# daemon.json
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonInfo:
    port: int
    secret: str
    pid: int
    version: str
    config_digest: str


def write_daemon_json(
    directory: Path,
    info: DaemonInfo,
    *,
    attempts: int = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Atomically, created ``0600``. ``os.replace`` is retried on
    ``PermissionError``: on Windows an open reader blocks it."""
    tmp = directory / f".{INFO_FILE}.{os.getpid()}.{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(asdict(info)).encode())
        for attempt in range(attempts):
            try:
                os.replace(tmp, directory / INFO_FILE)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                sleep(0.05)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def read_daemon_json(directory: Path) -> DaemonInfo | None:
    try:
        with open(directory / INFO_FILE, "rb") as f:
            raw: Any = json.loads(f.read(_MAX_INFO_BYTES))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    fields = {"port": int, "secret": str, "pid": int, "version": str, "config_digest": str}
    if set(raw) != set(fields):
        return None
    for name, kind in fields.items():
        value = raw[name]
        if type(value) is not kind:
            return None
    return DaemonInfo(**raw)


def remove_daemon_json(directory: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(directory / INFO_FILE)


# --------------------------------------------------------------------------
# Handshake and digest
# --------------------------------------------------------------------------


def hmac_response(secret: str, nonce: str) -> str:
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def verify_ping(secret: str, nonce: str, answer: str) -> bool:
    return hmac.compare_digest(hmac_response(secret, nonce).encode(), answer.encode())


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def config_digest(config_path: Path) -> str:
    """SHA-256 over the config file's bytes, then the token file's; a missing
    file counts as empty."""
    config = _read_bytes(config_path)
    token = b""
    try:
        value = tomllib.loads(config.decode()).get("push_token_file")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        value = None
    if isinstance(value, str):
        # As ``CodexConfig.load`` resolves it.
        token = _read_bytes(config_path.parent.absolute() / Path(value).expanduser())
    return hashlib.sha256(config + b"\0" + token).hexdigest()


# --------------------------------------------------------------------------
# Spawn backoff
# --------------------------------------------------------------------------


def record_spawn_failure(directory: Path, key: str, *, now: float | None = None) -> None:
    """``key`` names the version and config that failed; another one is not
    held back."""
    with contextlib.suppress(OSError):
        (directory / SPAWN_FAILED_FILE).write_text(f"{time.time() if now is None else now} {key}")


def clear_spawn_failure(directory: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(directory / SPAWN_FAILED_FILE)


def in_backoff(directory: Path, key: str, *, now: float | None = None) -> bool:
    try:
        stamp, _, failed_key = (directory / SPAWN_FAILED_FILE).read_text().partition(" ")
        failed_at = float(stamp)
    except (OSError, ValueError):
        return False
    elapsed = (time.time() if now is None else now) - failed_at
    return failed_key == key and 0 <= elapsed < SPAWN_BACKOFF_S


def spawn_key(config_path: Path) -> str:
    return f"{package_version()}:{config_digest(config_path)}"


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------


class DaemonLock:
    """An exclusive lock on ``daemon.lock``, held by the daemon for its life."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def acquire_lock(path: Path, *, wait: float) -> DaemonLock | None:
    """``None`` if another process still holds it after ``wait`` seconds."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    give_up = time.monotonic() + wait
    while not _try_lock(fd):
        if time.monotonic() >= give_up:
            os.close(fd)
            return None
        time.sleep(LOCK_POLL_S)
    return DaemonLock(fd)


def lock_held(path: Path) -> bool:
    """Whether a live process holds the lock (a dead one's lock is released)."""
    try:
        lock = acquire_lock(path, wait=0)
    except OSError:
        return False
    if lock is None:
        return True
    lock.release()
    return False


# --------------------------------------------------------------------------
# Talking to the daemon
# --------------------------------------------------------------------------


class DaemonError(Exception):
    """The connection broke, or the daemon answered something unusable."""


class DaemonGone(DaemonError):
    """The connection closed before any answer: the daemon exited or is
    exiting. Asking again, after discovery, is safe."""


class DaemonConnection:
    """A kept-alive connection whose peer answered ``/ping`` with the secret's
    HMAC; only then does the secret go out. It never reconnects: whoever holds
    the port then is unverified."""

    def __init__(self, conn: http.client.HTTPConnection, info: DaemonInfo) -> None:
        conn.auto_open = 0
        self._conn = conn
        self.info = info

    def post(self, path: str, body: bytes, *, deadline: float) -> tuple[int, bytes]:
        """Raises ``DaemonError``, ``TimeoutError`` at ``deadline`` (monotonic)."""
        try:
            _set_timeout(self._conn, deadline)
            self._conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": f"Bearer {self.info.secret}",
                    "Content-Type": "application/json",
                },
            )
            _set_timeout(self._conn, deadline)
            response = self._conn.getresponse()
        except TimeoutError:
            raise
        except (http.client.NotConnected, ConnectionError) as exc:
            # ``RemoteDisconnected`` is a ``ConnectionResetError``.
            raise DaemonGone(repr(exc)) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise DaemonError(repr(exc)) from exc
        try:
            return response.status, response.read()
        except TimeoutError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise DaemonError(repr(exc)) from exc

    def close(self) -> None:
        self._conn.close()


def _set_timeout(conn: http.client.HTTPConnection, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline exceeded")
    conn.timeout = remaining
    if conn.sock is not None:
        conn.sock.settimeout(remaining)


def connect(info: DaemonInfo, *, deadline: float) -> DaemonConnection | None:
    """``None`` unless the peer proves it knows the secret before ``deadline``
    on a connection it keeps open (one it closes, as an exiting daemon does,
    would be reopened to whoever holds the port next). Raises
    ``TimeoutError`` if the peer is silent until ``deadline``."""
    conn = http.client.HTTPConnection("127.0.0.1", info.port)
    nonce = secrets.token_hex(16)
    try:
        _set_timeout(conn, deadline)
        conn.request("GET", f"/ping?nonce={nonce}")
        _set_timeout(conn, deadline)
        response = conn.getresponse()
        answer = response.read(_MAX_PING_BYTES)
        complete = response.isclosed() or response.length == 0
    except TimeoutError:
        conn.close()
        raise
    except (OSError, http.client.HTTPException):
        conn.close()
        return None
    if (
        response.status != 200
        or response.will_close
        or not complete
        or not verify_ping(info.secret, nonce, answer.decode("ascii", "replace"))
    ):
        conn.close()
        return None
    return DaemonConnection(conn, info)


# --------------------------------------------------------------------------
# Spawn
# --------------------------------------------------------------------------


def spawn_daemon(
    argv: list[str],
    *,
    cwd: Path,
    stderr_path: Path,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    platform: str = sys.platform,
) -> subprocess.Popen[bytes]:
    """Detached and holding none of the hook's pipes, or Codex would wait on it."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    output: IO[bytes] = os.fdopen(os.open(stderr_path, flags, 0o600), "ab")
    with output:
        common: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": output,
            "stderr": output,
            "close_fds": True,
            "cwd": cwd,
        }
        if platform != "win32":
            return popen(argv, start_new_session=True, **common)
        detached = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            return popen(argv, creationflags=detached | CREATE_BREAKAWAY_FROM_JOB, **common)
        except OSError:
            # The job forbids breakaway.
            return popen(argv, creationflags=detached, **common)
