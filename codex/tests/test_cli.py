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
from typing import Any, BinaryIO

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


def _no_exit(code: int) -> None:
    raise AssertionError(f"backstop exit({code})")


def _hook(event: str, daemons: Daemons, stdin: BinaryIO, **kwargs: Any) -> str:
    """``run_hook`` in process; its one answer."""
    written: list[str] = []
    run_hook(
        event,
        daemons.config,
        daemons.state,
        daemons.codex_home,
        stdin,
        write=written.append,
        exit=_no_exit,
        **kwargs,
    )
    [out] = written
    return out


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
    # Output apart from the rotated log; nothing crashed.
    assert (daemons.state / "daemon.stderr").read_bytes() == b""


def test_hook_path_imports_stdlib_only(daemons: Daemons) -> None:
    daemons.start()
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
    old = daemons.start()
    write_daemon_json(
        daemons.state,
        DaemonInfo(old.port, old.secret, old.pid, "0.0.0-old", old.config_digest),
    )
    _restarted(daemons, old)


def test_config_change_restarts(daemons: Daemons) -> None:
    old = daemons.start()
    daemons.write_config(daemon_idle_seconds=601)
    _restarted(daemons, old)


def test_token_change_restarts(daemons: Daemons) -> None:
    old = daemons.start()
    (daemons.root / "token").write_text("u" * 32)
    _restarted(daemons, old)


@pytest.mark.parametrize("mode", ["deny", "allow"])
def test_daemon_cannot_start(daemons: Daemons, mode: str) -> None:
    daemons.write_config(push_token_file=str(daemons.root / "missing"), verdict_fail_mode=mode)
    first = _out(daemons.hook("UserPromptSubmit", UPS))
    assert first == (BLOCK_UNAVAILABLE if mode == "deny" else {})
    assert in_backoff(daemons.state, spawn_key(daemons.config))
    assert daemons.log().count("starting") == 1
    spawned: list[list[str]] = []

    def spawn(argv: list[str], *_: Path) -> FakeProcess:
        spawned.append(argv)
        return FakeProcess()

    assert json.loads(_hook("UserPromptSubmit", daemons, io.BytesIO(UPS), spawn=spawn)) == first
    # Triggers do not spawn during the backoff either.
    assert json.loads(_hook("Stop", daemons, io.BytesIO(STOP), spawn=spawn)) == {}
    assert spawned == []


def test_daemon_started_during_backoff_used(daemons: Daemons) -> None:
    daemons.state.mkdir(parents=True)
    record_spawn_failure(daemons.state, spawn_key(daemons.config))
    info = daemons.start()
    assert not (daemons.state / "spawn-failed").exists()
    assert _out(daemons.hook("UserPromptSubmit", UPS)) == {}
    assert daemons.info() == info


def test_trigger_spawns_without_waiting(daemons: Daemons) -> None:
    assert _out(daemons.hook("Stop", STOP)) == {}
    info = daemons.wait_info()
    assert alive(info.pid)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="counts daemons in /proc")
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


def test_bad_arguments_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "slashid_codex", "hook", "--event", "PreToolUse"],
        input=b"{}",
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"decision": "block", "reason": cli.BAD_ARGUMENTS}


@pytest.mark.parametrize(
    ("event", "mode", "blocks"),
    [
        ("UserPromptSubmit", "deny", True),
        ("PreToolUse", "deny", True),
        ("PreToolUse", "allow", False),
        ("PreToolUse", None, True),
        # ``block`` on ``Stop`` would continue the turn.
        ("Stop", "deny", False),
        ("SessionStart", "deny", False),
        ("SessionEnd", "deny", False),
        ("Nope", "deny", False),
        (None, "deny", False),
    ],
)
@pytest.mark.parametrize("equals", [False, True])
def test_bad_arguments(
    daemons: Daemons,
    capsys: pytest.CaptureFixture[str],
    event: str | None,
    mode: str | None,
    blocks: bool,
    equals: bool,
) -> None:
    if mode is not None:
        daemons.write_config(verdict_fail_mode=mode)
    config = str(daemons.config if mode is not None else daemons.root / "missing.toml")
    argv = ["hook", "--bogus"]
    if equals:
        argv += [f"--config={config}"] + ([f"--event={event}"] if event else [])
    else:
        argv += ["--config", config] + (["--event", event] if event else [])
    assert cli.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == ({"decision": "block", "reason": cli.BAD_ARGUMENTS} if blocks else {})


def test_stdin_over_the_cap_drained(daemons: Daemons) -> None:
    stdin = io.BytesIO(b" " * (cli.MAX_STDIN_BYTES + 5 * 65536))
    out = _hook("PreToolUse", daemons, stdin)
    assert json.loads(out) == {"decision": "block", "reason": PAYLOAD_TOO_LARGE}
    assert stdin.read() == b""


# --------------------------------------------------------------------------
# A fake daemon, in process
# --------------------------------------------------------------------------


class FakeDaemon:
    """Answers ``/ping`` correctly; ``/hooks/*`` per ``mode``."""

    def __init__(self, state: Path, config: Path, mode: str, *, ping_delay: float = 0) -> None:
        self.release = threading.Event()
        self.secret = "f" * 64
        self.posts = 0
        self.pings = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                fake.pings += 1
                fake.release.wait(ping_delay)
                nonce = self.path.partition("nonce=")[2]
                body = hmac_response(fake.secret, nonce).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                fake.posts += 1
                if mode == "slow":
                    fake.release.wait(30)
                elif mode == "trickle":
                    # Each byte inside any per-operation timeout.
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                    while not fake.release.wait(0.05):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                elif mode == "allow":
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
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
        out = _hook(event, daemons, io.BytesIO(UPS), deadlines={event: 0.3})
        elapsed = time.monotonic() - start
    finally:
        fake.close()
    assert 0.25 <= elapsed < 1.0
    if event in cli.PREFLIGHT_EVENTS and mode == "deny":
        assert json.loads(out) == {"decision": "block", "reason": DAEMON_TIMEOUT}
    else:
        assert json.loads(out) == {}


class Backstop:
    """``run_hook`` on a thread, with its writes and exits recorded."""

    def __init__(self, event: str, daemons: Daemons, stdin: BinaryIO, deadline: float) -> None:
        self.written: list[str] = []
        self.exits: list[int] = []
        self.exited = threading.Event()
        self.start = time.monotonic()
        self.elapsed = 0.0

        def exit(code: int) -> None:
            self.elapsed = time.monotonic() - self.start
            self.exits.append(code)
            self.exited.set()

        self.thread = threading.Thread(
            target=run_hook,
            args=(event, daemons.config, daemons.state, daemons.codex_home, stdin),
            kwargs={"deadlines": {event: deadline}, "write": self.written.append, "exit": exit},
            daemon=True,
        )
        self.thread.start()


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop"])
@pytest.mark.parametrize("mode", ["deny", "allow"])
def test_trickling_daemon_cut_off_at_the_deadline(daemons: Daemons, event: str, mode: str) -> None:
    daemons.write_config(verdict_fail_mode=mode)
    fake = FakeDaemon(daemons.state, daemons.config, "trickle")
    try:
        run = Backstop(event, daemons, io.BytesIO(UPS), 0.4)
        assert run.exited.wait(5)
    finally:
        fake.close()
    run.thread.join(5)
    assert 0.35 <= run.elapsed < 1.0
    assert run.exits == [0]
    # The client's own answer, once the trickle stops, is not printed.
    [out] = run.written
    if event == "UserPromptSubmit" and mode == "deny":
        assert json.loads(out) == {"decision": "block", "reason": cli.HOOK_TIMEOUT}
    else:
        assert json.loads(out) == {}


def test_stdin_never_closed_cut_off_at_the_deadline(daemons: Daemons) -> None:
    read, write = os.pipe()
    try:
        with open(read, "rb", buffering=0) as stdin:
            run = Backstop("PreToolUse", daemons, stdin, 0.3)
            assert run.exited.wait(5)
            os.close(write)
            run.thread.join(5)
    finally:
        with contextlib.suppress(OSError):
            os.close(write)
    assert run.exits == [0]
    assert [json.loads(o) for o in run.written] == [
        {"decision": "block", "reason": cli.HOOK_TIMEOUT}
    ]


def test_answer_in_time_cancels_the_backstop(daemons: Daemons) -> None:
    fake = FakeDaemon(daemons.state, daemons.config, "allow")
    try:
        run = Backstop("PreToolUse", daemons, io.BytesIO(UPS), 0.3)
        run.thread.join(5)
        assert not run.exited.wait(0.5)
    finally:
        fake.close()
    assert run.written == ["{}"]


def test_slow_ping_from_a_live_daemon_retried_not_respawned(daemons: Daemons) -> None:
    daemons.state.mkdir(parents=True)
    lock = acquire_lock(daemons.state / "daemon.lock", wait=0)
    assert lock is not None
    fake = FakeDaemon(daemons.state, daemons.config, "allow", ping_delay=cli.PING_TIMEOUT_S + 0.1)
    spawned: list[list[str]] = []
    try:
        out = _hook(
            "PreToolUse",
            daemons,
            io.BytesIO(UPS),
            spawn=lambda argv, *_: spawned.append(argv) or FakeProcess(),
        )
    finally:
        fake.close()
        lock.release()
    assert json.loads(out) == {}
    assert spawned == []
    assert fake.pings == 2


def test_silent_live_daemon_not_respawned(daemons: Daemons) -> None:
    daemons.state.mkdir(parents=True)
    lock = acquire_lock(daemons.state / "daemon.lock", wait=0)
    assert lock is not None
    fake = FakeDaemon(daemons.state, daemons.config, "allow", ping_delay=30)
    spawned: list[list[str]] = []
    try:
        out = _hook(
            "PreToolUse",
            daemons,
            io.BytesIO(UPS),
            deadlines={"PreToolUse": 1.3},
            spawn=lambda argv, *_: spawned.append(argv) or FakeProcess(),
        )
    finally:
        fake.close()
        lock.release()
    assert json.loads(out) == BLOCK_UNAVAILABLE
    assert spawned == []


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
        out = _hook("PreToolUse", daemons, io.BytesIO(UPS))
    finally:
        fake.close()
    assert json.loads(out) == {"decision": "block", "reason": DAEMON_BROKEN}
    # Closed with no answer: asked again once, after discovery.
    assert fake.posts == 2


class FakeProcess:
    def __init__(self, code: int | None = None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


def test_trigger_spawn_does_not_wait(daemons: Daemons) -> None:
    spawned: list[list[str]] = []

    def spawn(argv: list[str], cwd: Path, stderr_path: Path) -> FakeProcess:
        spawned.append(argv)
        return FakeProcess()

    start = time.monotonic()
    out = _hook("SessionEnd", daemons, io.BytesIO(b"{}"), spawn=spawn)
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


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_spawn_polled_for_5_s(daemons: Daemons) -> None:
    daemons.state.mkdir(parents=True)
    clock = FakeClock()
    client = Client(
        daemons.config,
        daemons.state,
        None,
        spawn=lambda *_: FakeProcess(),
        monotonic=clock,
        sleep=clock.sleep,
    )
    assert client.connect(9.0, wait=True) is None
    assert 5.0 <= clock.now < 5.0 + 2 * cli.POLL_S
    assert in_backoff(daemons.state, spawn_key(daemons.config))


def test_spawn_wait_inside_the_preflight_deadline() -> None:
    assert cli.SPAWN_WAIT_S == 5.0
    budget = cli.PING_TIMEOUT_S + cli.SHUTDOWN_TIMEOUT_S + cli.SPAWN_WAIT_S
    assert budget < DEADLINES["PreToolUse"]


def test_spawn_holding_the_lock_is_not_a_failure(daemons: Daemons) -> None:
    daemons.state.mkdir(parents=True)
    lock = acquire_lock(daemons.state / "daemon.lock", wait=0)
    assert lock is not None
    clock = FakeClock()
    try:
        client = Client(
            daemons.config,
            daemons.state,
            None,
            spawn=lambda *_: FakeProcess(),
            monotonic=clock,
            sleep=clock.sleep,
        )
        assert client.connect(9.0, wait=True) is None
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


def _common(kwargs: dict[str, Any], stderr_path: Path) -> None:
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["stdout"] is kwargs["stderr"]
    assert kwargs["stdout"] not in (None, subprocess.PIPE)
    assert stderr_path.exists()
    assert not (stderr_path.parent / "daemon.log").exists()


def test_spawn_flags_posix(tmp_path: Path) -> None:
    popen = Popen()
    spawn_daemon(
        ["x"], cwd=tmp_path, stderr_path=tmp_path / "daemon.stderr", popen=popen, platform="linux"
    )
    [kwargs] = popen.calls
    _common(kwargs, tmp_path / "daemon.stderr")
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


@pytest.mark.parametrize("fail_breakaway", [False, True])
def test_spawn_flags_windows(tmp_path: Path, fail_breakaway: bool) -> None:
    popen = Popen(fail_breakaway)
    spawn_daemon(
        ["x"], cwd=tmp_path, stderr_path=tmp_path / "daemon.stderr", popen=popen, platform="win32"
    )
    detached = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    flags = [c["creationflags"] for c in popen.calls]
    if fail_breakaway:
        assert flags == [detached | CREATE_BREAKAWAY_FROM_JOB, detached]
    else:
        assert flags == [detached | CREATE_BREAKAWAY_FROM_JOB]
    for kwargs in popen.calls:
        _common(kwargs, tmp_path / "daemon.stderr")
        assert "start_new_session" not in kwargs
