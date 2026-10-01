from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import multiprocessing
import multiprocessing.synchronize
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import platformdirs
import pytest

from slashid_codex import discovery
from slashid_codex.discovery import (
    DaemonError,
    DaemonInfo,
    acquire_lock,
    config_digest,
    connect,
    hmac_response,
    in_backoff,
    lock_held,
    package_version,
    read_daemon_json,
    record_spawn_failure,
    spawn_key,
    state_dir,
    verify_ping,
    write_daemon_json,
)

INFO = DaemonInfo(port=4242, secret="s" * 64, pid=123, version="1.0", config_digest="d" * 64)


def test_state_dir_default_and_override(tmp_path: Path) -> None:
    assert state_dir() == Path(platformdirs.user_data_dir("slashid-ai-forwarder-codex", "slashid"))
    assert state_dir(tmp_path) == tmp_path


def test_daemon_json_round_trip(tmp_path: Path) -> None:
    write_daemon_json(tmp_path, INFO)
    assert read_daemon_json(tmp_path) == INFO
    assert not [p for p in tmp_path.iterdir() if p.name != "daemon.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_daemon_json_mode(tmp_path: Path) -> None:
    write_daemon_json(tmp_path, INFO)
    assert (tmp_path / "daemon.json").stat().st_mode & 0o777 == 0o600


def test_daemon_json_replace_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.replace
    calls: list[int] = []

    def flaky(src: str, dst: str) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("held by a reader")
        real(src, dst)

    monkeypatch.setattr(discovery.os, "replace", flaky)
    write_daemon_json(tmp_path, INFO, sleep=lambda _: None)
    assert len(calls) == 3
    assert read_daemon_json(tmp_path) == INFO


def test_daemon_json_replace_gives_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(src: str, dst: str) -> None:
        raise PermissionError("held")

    monkeypatch.setattr(discovery.os, "replace", denied)
    with pytest.raises(PermissionError):
        write_daemon_json(tmp_path, INFO, sleep=lambda _: None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "content",
    ["", "{", "[]", '{"port": "x"}', json.dumps({**INFO.__dict__, "pid": True})],
)
def test_daemon_json_invalid(tmp_path: Path, content: str) -> None:
    (tmp_path / "daemon.json").write_text(content)
    assert read_daemon_json(tmp_path) is None


def test_daemon_json_missing(tmp_path: Path) -> None:
    assert read_daemon_json(tmp_path) is None


def test_hmac_and_verify() -> None:
    answer = hmac_response("secret", "nonce")
    assert answer == hmac.new(b"secret", b"nonce", hashlib.sha256).hexdigest()
    assert verify_ping("secret", "nonce", answer)
    assert not verify_ping("other", "nonce", answer)
    assert not verify_ping("secret", "other", answer)


def test_config_digest(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_bytes(b"t" * 32)
    config = tmp_path / "config.toml"
    config.write_bytes(b'push_token_file = "token"\n')
    expected = hashlib.sha256(config.read_bytes() + b"\0" + token.read_bytes()).hexdigest()
    assert config_digest(config) == expected
    token.write_bytes(b"u" * 32)
    assert config_digest(config) != expected


def test_config_digest_absolute_and_missing(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'push_token_file = "{tmp_path / "nope"}"\n')
    assert config_digest(config) == hashlib.sha256(config.read_bytes() + b"\0").hexdigest()
    assert config_digest(tmp_path / "absent.toml") == hashlib.sha256(b"\0").hexdigest()


def test_spawn_backoff(tmp_path: Path) -> None:
    assert not in_backoff(tmp_path, "k", now=1000.0)
    record_spawn_failure(tmp_path, "k", now=1000.0)
    assert in_backoff(tmp_path, "k", now=1000.0)
    assert in_backoff(tmp_path, "k", now=1299.0)
    assert not in_backoff(tmp_path, "k", now=1300.0)
    # A clock moved back does not extend it forever.
    assert not in_backoff(tmp_path, "k", now=900.0)
    # A new version, config or token is tried at once.
    assert not in_backoff(tmp_path, "other", now=1000.0)
    (tmp_path / "spawn-failed").write_text("garbage")
    assert not in_backoff(tmp_path, "k", now=1000.0)


def test_spawn_key(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")
    assert spawn_key(config) == f"{package_version()}:{config_digest(config)}"


def _hold(path: str, ready: multiprocessing.synchronize.Event, release: float) -> None:
    lock = acquire_lock(Path(path), wait=0)
    assert lock is not None
    ready.set()
    time.sleep(release)
    lock.release()


def test_lock_exclusive_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(path), ready, 1.0))
    holder.start()
    try:
        assert ready.wait(20)
        assert lock_held(path)
        start = time.monotonic()
        assert acquire_lock(path, wait=0.2) is None
        assert time.monotonic() - start >= 0.2
        # Waits for the holder to let go.
        lock = acquire_lock(path, wait=10)
        assert lock is not None
        assert lock_held(path)
        lock.release()
        assert not lock_held(path)
    finally:
        holder.join(10)


def test_client_imports_stdlib_only() -> None:
    # Modules a site ``.pth`` file loads at startup are not the client's.
    code = (
        "import sys, json\n"
        "before = set(sys.modules)\n"
        "import slashid_codex.discovery\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in set(sys.modules) - before})))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=True, text=True
    ).stdout
    third_party = {
        m for m in json.loads(out) if m not in sys.stdlib_module_names and not m.startswith("_")
    }
    assert third_party <= {"slashid_codex", "platformdirs"}


class _ClosingDaemon:
    """Answers ``/ping`` correctly, then ``posts`` POSTs; the last answer says
    ``Connection: close`` (as uvicorn does while shutting down). Then it frees
    the port to a squatter that records what reaches it."""

    def __init__(self, secret: str, *, posts: int) -> None:
        self.received = b""
        self.accepted = threading.Event()
        self.squatting = threading.Event()
        self._posts = posts
        self._secret = secret
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self.port: int = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self._listener.accept()
        buffered = b""
        for answered in range(self._posts + 1):
            while b"\r\n\r\n" not in buffered:
                buffered += conn.recv(65536)
            head, _, buffered = buffered.partition(b"\r\n\r\n")
            length = int(
                next(
                    (
                        h.split(b":")[1]
                        for h in head.split(b"\r\n")
                        if h.lower().startswith(b"content-length")
                    ),
                    b"0",
                )
            )
            while len(buffered) < length:
                buffered += conn.recv(65536)
            buffered = buffered[length:]
            if answered == 0:
                nonce = head.split(b" ")[1].partition(b"nonce=")[2].decode()
                body = hmac_response(self._secret, nonce).encode()
            else:
                body = b"{}"
            close = b"Connection: close\r\n" if answered == self._posts else b""
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n%s\r\n" % (len(body), close) + body
            )
        conn.close()
        self._listener.close()
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", self.port))
        squatter.listen()
        squatter.settimeout(2)
        self.squatting.set()
        with squatter, contextlib.suppress(OSError):
            peer, _ = squatter.accept()
            self.accepted.set()
            with peer:
                peer.settimeout(1)
                while chunk := peer.recv(65536):
                    self.received += chunk
                    peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")


def test_ping_with_connection_close_fails_the_handshake() -> None:
    fake = _ClosingDaemon("a" * 64, posts=0)
    assert (
        connect(DaemonInfo(fake.port, "a" * 64, 1, "v", "d"), deadline=time.monotonic() + 3) is None
    )
    assert fake.squatting.wait(3)
    assert not fake.accepted.wait(0.5)
    assert fake.received == b""


def test_verified_connection_never_reopens() -> None:
    fake = _ClosingDaemon("a" * 64, posts=1)
    conn = connect(DaemonInfo(fake.port, "a" * 64, 1, "v", "d"), deadline=time.monotonic() + 3)
    assert conn is not None
    assert conn.post("/hooks/Stop", b"{}", deadline=time.monotonic() + 3) == (200, b"{}")
    assert fake.squatting.wait(3)
    with pytest.raises(DaemonError):
        conn.post("/hooks/UserPromptSubmit", b'{"prompt": "x"}', deadline=time.monotonic() + 3)
    assert not fake.accepted.wait(0.5)
    assert fake.received == b""
