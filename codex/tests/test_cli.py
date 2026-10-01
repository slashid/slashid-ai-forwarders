from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from slashid_codex import cli
from slashid_codex.cli import (
    DAEMON_BROKEN,
    DAEMON_TIMEOUT,
    DAEMON_UNAVAILABLE,
    DEADLINES,
    PAYLOAD_TOO_LARGE,
    Client,
    run_hook,
)
from slashid_codex.discovery import (
    CREATE_BREAKAWAY_FROM_JOB,
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    DaemonInfo,
    acquire_lock,
    config_digest,
    hmac_response,
    in_backoff,
    package_version,
    record_spawn_failure,
    spawn_daemon,
    spawn_key,
    write_daemon_json,
)
from tests.daemons import Daemons, alive, wait_dead

HOOKS = Path(__file__).parent / "fixtures" / "hooks"
UPS = (HOOKS / "user_prompt_submit.json").read_bytes()
STOP = (HOOKS / "stop.json").read_bytes()
BLOCK_UNAVAILABLE = {"decision": "block", "reason": DAEMON_UNAVAILABLE}


@pytest.fixture
def daemons(tmp_path: Path) -> Iterator[Daemons]:
    env = Daemons(tmp_path)
    try:
        yield env
    finally:
        env.kill_all()


def _out(result: subprocess.CompletedProcess[bytes]) -> Any:
    assert result.returncode == 0
    return json.loads(result.stdout)


# --------------------------------------------------------------------------
# Real daemons
# --------------------------------------------------------------------------


def test_first_call_spawns_second_reuses(daemons: Daemons) -> None:
    # ``capture_output``: a daemon holding the hook's pipes would block this past the timeout.
    assert _out(daemons.hook("UserPromptSubmit", UPS, timeout=15)) == {}
    info = daemons.info()
    assert info is not None
    assert alive(info.pid)
    assert (
        _out(daemons.hook("PreToolUse", (HOOKS / "pre_tool_use_bash_sed.json").read_bytes())) == {}
    )
    assert daemons.info() == info
    assert daemons.pids() == [info.pid]
    assert "dry run preflight" in daemons.log()


def test_hook_path_imports_stdlib_only(daemons: Daemons) -> None:
    assert _out(daemons.hook("Stop", STOP)) == {}
    daemons.wait_info()
    hook_args = daemons.args("hook", "--event", "UserPromptSubmit")[3:]
    code = (
        "import io, json, sys\n"
        "before = set(sys.modules)\n"
        "from slashid_codex import cli\n"
        f"sys.stdin = io.TextIOWrapper(io.BytesIO({UPS!r}))\n"
        "out = io.StringIO(); real = sys.stdout; sys.stdout = out\n"
        f"cli.main({hook_args!r})\n"
        "sys.stdout = real\n"
        "mods = sorted({m.split('.')[0] for m in set(sys.modules) - before})\n"
        "print(json.dumps({'out': out.getvalue(), 'mods': mods}))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True)
    report = json.loads(result.stdout)
    assert json.loads(report["out"]) == {}
    third_party = {
        m for m in report["mods"] if m not in sys.stdlib_module_names and not m.startswith("_")
    }
    assert third_party <= {"slashid_codex", "platformdirs"}


def test_stale_daemon_json_replaced(daemons: Daemons) -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    daemons.state.mkdir(parents=True)
    stale = DaemonInfo(port, "s" * 64, 2**22 + 12345, package_version(), "x")
    write_daemon_json(daemons.state, stale)
    assert _out(daemons.hook("UserPromptSubmit", UPS)) == {}
    info = daemons.info()
    assert info is not None
    assert info.port != port
    assert not (daemons.state / "spawn-failed").exists()


class _Squatter:
    """A plain TCP listener that cannot answer the HMAC."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port: int = self.sock.getsockname()[1]
        self.received = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = self.sock.accept()
                with conn:
                    conn.settimeout(2)
                    with contextlib.suppress(OSError):
                        while b"\r\n\r\n" not in self.received:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            self.received += chunk
                        body = b"0" * 64
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 64\r\n\r\n" + body)
                        while chunk := conn.recv(65536):
                            self.received += chunk

    def close(self) -> None:
        self.sock.close()


def test_squatted_port_receives_nothing(daemons: Daemons) -> None:
    squatter = _Squatter()
    try:
        daemons.state.mkdir(parents=True)
        write_daemon_json(
            daemons.state,
            DaemonInfo(
                squatter.port,
                "s" * 64,
                os.getpid(),
                package_version(),
                config_digest(daemons.config),
            ),
        )
        assert _out(daemons.hook("UserPromptSubmit", UPS)) == {}
        info = daemons.info()
        assert info is not None
        assert info.port != squatter.port
        head, _, rest = squatter.received.partition(b"\r\n\r\n")
        assert head.startswith(b"GET /ping?nonce=")
        assert b"Authorization" not in head
        assert b"s" * 64 not in squatter.received
        assert rest == b""
    finally:
        squatter.close()


def _restarted(daemons: Daemons, old: DaemonInfo) -> None:
    assert _out(daemons.hook("UserPromptSubmit", UPS)) == {}
    new = daemons.info()
    assert new is not None
    assert new.pid != old.pid
    assert wait_dead(old.pid)
    assert not (daemons.state / "spawn-failed").exists()
    assert daemons.pids() == [new.pid]


def test_version_mismatch_restarts(daemons: Daemons) -> None:
    assert _out(daemons.hook("Stop", STOP)) == {}
    old = daemons.wait_info()
    write_daemon_json(
        daemons.state,
        DaemonInfo(old.port, old.secret, old.pid, "0.0.0-old", old.config_digest),
    )
    _restarted(daemons, old)


def test_config_change_restarts(daemons: Daemons) -> None:
    assert _out(daemons.hook("Stop", STOP)) == {}
    old = daemons.wait_info()
    daemons.write_config(daemon_idle_seconds=601)
    _restarted(daemons, old)


def test_token_change_restarts(daemons: Daemons) -> None:
    assert _out(daemons.hook("Stop", STOP)) == {}
    old = daemons.wait_info()
    (daemons.root / "token").write_text("u" * 32)
    _restarted(daemons, old)


@pytest.mark.parametrize("mode", ["deny", "allow"])
def test_daemon_cannot_start(daemons: Daemons, mode: str) -> None:
    daemons.write_config(push_token_file=str(daemons.root / "missing"), verdict_fail_mode=mode)
    first = _out(daemons.hook("UserPromptSubmit", UPS))
    assert first == (BLOCK_UNAVAILABLE if mode == "deny" else {})
    assert in_backoff(daemons.state, spawn_key(daemons.config))
    starts = daemons.log().count("starting")
    assert starts == 1
    assert _out(daemons.hook("UserPromptSubmit", UPS)) == first
    # Triggers do not spawn during the backoff either.
    assert _out(daemons.hook("Stop", STOP)) == {}
    time.sleep(0.5)
    assert daemons.log().count("starting") == 1


def test_daemon_started_during_backoff_used(daemons: Daemons) -> None:
    daemons.state.mkdir(parents=True)
    record_spawn_failure(daemons.state, spawn_key(daemons.config))
    process = subprocess.Popen(
        daemons.args("daemon"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    info = daemons.wait_info()
    assert info.pid == process.pid
    assert not (daemons.state / "spawn-failed").exists()
    assert _out(daemons.hook("UserPromptSubmit", UPS)) == {}
    assert daemons.info() == info


def test_trigger_spawns_without_waiting(daemons: Daemons) -> None:
    assert _out(daemons.hook("Stop", STOP)) == {}
    info = daemons.wait_info()
    assert alive(info.pid)


def test_racing_starts_one_daemon(daemons: Daemons) -> None:
    procs = [
        subprocess.Popen(
            daemons.args("hook", "--event", "UserPromptSubmit"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(UPS, timeout=30)[0] for p in procs]
    assert [json.loads(o) for o in outs] == [{}, {}]
    info = daemons.info()
    assert info is not None
    # The loser waits out the lock, then exits.
    give_up = time.monotonic() + 10
    while daemons.pids() != [info.pid] and time.monotonic() < give_up:
        time.sleep(0.1)
    assert daemons.pids() == [info.pid]
    assert not (daemons.state / "spawn-failed").exists()


def test_stdin_too_large(daemons: Daemons) -> None:
    payload = b" " * (10 * 1024 * 1024 + 1)
    out = _out(daemons.hook("PreToolUse", payload))
    assert out == {"decision": "block", "reason": PAYLOAD_TOO_LARGE}
    assert daemons.info() is None


def test_bad_arguments_block(daemons: Daemons) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "slashid_codex", "hook", "--event", "Nope"],
        input=b"{}",
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"


# --------------------------------------------------------------------------
# A fake daemon, in process
# --------------------------------------------------------------------------


class FakeDaemon:
    """Answers ``/ping`` correctly; ``/hooks/*`` per ``mode``."""

    def __init__(self, state: Path, config: Path, mode: str) -> None:
        self.release = threading.Event()
        self.secret = "f" * 64
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                nonce = self.path.partition("nonce=")[2]
                body = hmac_response(fake.secret, nonce).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                if mode == "slow":
                    fake.release.wait(30)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        state.mkdir(parents=True, exist_ok=True)
        write_daemon_json(
            state,
            DaemonInfo(
                self.server.server_address[1],
                self.secret,
                os.getpid(),
                package_version(),
                config_digest(config),
            ),
        )

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("event", list(DEADLINES))
@pytest.mark.parametrize("mode", ["deny", "allow"])
def test_slow_daemon_answered_at_deadline(daemons: Daemons, event: str, mode: str) -> None:
    daemons.write_config(verdict_fail_mode=mode)
    fake = FakeDaemon(daemons.state, daemons.config, "slow")
    try:
        start = time.monotonic()
        out = run_hook(
            event,
            daemons.config,
            daemons.state,
            daemons.codex_home,
            io.BytesIO(UPS),
            deadlines={event: 0.3},
        )
        elapsed = time.monotonic() - start
    finally:
        fake.close()
    assert 0.25 <= elapsed < 1.0
    if event in cli.PREFLIGHT_EVENTS and mode == "deny":
        assert json.loads(out) == {"decision": "block", "reason": DAEMON_TIMEOUT}
    else:
        assert json.loads(out) == {}


def test_deadlines() -> None:
    assert DEADLINES == {
        "UserPromptSubmit": 9.0,
        "PreToolUse": 9.0,
        "Stop": 4.0,
        "SessionStart": 4.0,
        "SessionEnd": 2.5,
    }


def test_connection_broken_mid_request(daemons: Daemons) -> None:
    fake = FakeDaemon(daemons.state, daemons.config, "close")
    try:
        out = run_hook(
            "PreToolUse", daemons.config, daemons.state, daemons.codex_home, io.BytesIO(UPS)
        )
    finally:
        fake.close()
    assert json.loads(out) == {"decision": "block", "reason": DAEMON_BROKEN}


class FakeProcess:
    def __init__(self, code: int | None = None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


def test_trigger_spawn_does_not_wait(daemons: Daemons) -> None:
    spawned: list[list[str]] = []

    def spawn(argv: list[str], cwd: Path, log_path: Path) -> FakeProcess:
        spawned.append(argv)
        return FakeProcess()

    start = time.monotonic()
    out = run_hook(
        "SessionEnd",
        daemons.config,
        daemons.state,
        daemons.codex_home,
        io.BytesIO(b"{}"),
        spawn=spawn,
    )
    assert time.monotonic() - start < 0.3
    assert json.loads(out) == {}
    [argv] = spawned
    assert argv[:4] == [sys.executable, "-m", "slashid_codex", "daemon"]
    assert argv[4:] == [
        "--config",
        str(daemons.config),
        "--state-dir",
        str(daemons.state),
        "--codex-home",
        str(daemons.codex_home),
    ]


def test_spawn_polled_for_1_5_s(daemons: Daemons) -> None:
    start = time.monotonic()
    out = run_hook(
        "UserPromptSubmit",
        daemons.config,
        daemons.state,
        daemons.codex_home,
        io.BytesIO(UPS),
        spawn=lambda *_: FakeProcess(),
    )
    assert 1.4 <= time.monotonic() - start < 2.5
    assert json.loads(out) == BLOCK_UNAVAILABLE
    assert in_backoff(daemons.state, spawn_key(daemons.config))


def test_spawn_holding_the_lock_is_not_a_failure(daemons: Daemons) -> None:
    daemons.state.mkdir(parents=True)
    lock = acquire_lock(daemons.state / "daemon.lock", wait=0)
    assert lock is not None
    try:
        client = Client(
            daemons.config,
            daemons.state,
            None,
            spawn=lambda *_: FakeProcess(),
            sleep=lambda _: None,
        )
        assert client.connect(time.monotonic() + 9, wait=True) is None
    finally:
        lock.release()
    assert not in_backoff(daemons.state, spawn_key(daemons.config))


def test_spawn_exit_error_is_a_failure(daemons: Daemons) -> None:
    daemons.state.mkdir(parents=True)
    client = Client(daemons.config, daemons.state, None, spawn=lambda *_: FakeProcess(2))
    start = time.monotonic()
    assert client.connect(time.monotonic() + 9, wait=True) is None
    assert time.monotonic() - start < 0.5
    assert in_backoff(daemons.state, spawn_key(daemons.config))


# --------------------------------------------------------------------------
# Spawn flags
# --------------------------------------------------------------------------


class Popen:
    def __init__(self, fail_breakaway: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_breakaway = fail_breakaway

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        flags = kwargs.get("creationflags", 0)
        if self.fail_breakaway and flags & CREATE_BREAKAWAY_FROM_JOB:
            raise OSError("access denied")
        return object()


def _common(kwargs: dict[str, Any], log_path: Path) -> None:
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["stdout"] is kwargs["stderr"]
    assert kwargs["stdout"] not in (None, subprocess.PIPE)
    assert log_path.exists()


def test_spawn_flags_posix(tmp_path: Path) -> None:
    popen = Popen()
    spawn_daemon(
        ["x"], cwd=tmp_path, log_path=tmp_path / "daemon.log", popen=popen, platform="linux"
    )
    [kwargs] = popen.calls
    _common(kwargs, tmp_path / "daemon.log")
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


@pytest.mark.parametrize("fail_breakaway", [False, True])
def test_spawn_flags_windows(tmp_path: Path, fail_breakaway: bool) -> None:
    popen = Popen(fail_breakaway)
    spawn_daemon(
        ["x"], cwd=tmp_path, log_path=tmp_path / "daemon.log", popen=popen, platform="win32"
    )
    detached = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    flags = [c["creationflags"] for c in popen.calls]
    if fail_breakaway:
        assert flags == [detached | CREATE_BREAKAWAY_FROM_JOB, detached]
    else:
        assert flags == [detached | CREATE_BREAKAWAY_FROM_JOB]
    for kwargs in popen.calls:
        _common(kwargs, tmp_path / "daemon.log")
        assert "start_new_session" not in kwargs
