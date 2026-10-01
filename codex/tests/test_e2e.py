"""Client and daemon subprocesses, ``dry_run = false``, against a stub SlashID
on plain HTTP: the real httpx path for preflight and push."""

from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from slashid_codex import cli
from slashid_codex.discovery import DaemonConnection, DaemonInfo, spawn_daemon
from slashid_codex.rollout import TokenUsageRecord, parse_line
from tests.daemons import TOKEN, Daemons, wait_dead
from tests.e2e_daemon import PlainHttpConfig

HOOKS = Path(__file__).parent / "fixtures" / "hooks"
ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
SESSION = "01a0f397-f16e-7d83-87e7-6701f1b384c7"
PACKAGE = Path(__file__).parents[1]


class StubSlashID:
    def __init__(self) -> None:
        self.deny: list[str] = []
        self.preflights: list[dict[str, Any]] = []
        self.pushed: list[dict[str, Any]] = []
        self.auth: set[str] = set()
        self.pushed_event = threading.Event()
        # Pushes to receive and never answer, and what they carried.
        self.cut_off = 0
        self.cut: list[dict[str, Any]] = []
        self.cut_event = threading.Event()
        self.release = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.auth.add(self.headers["Authorization"])
                if self.path == "/ip/nhi/events/ai-invocations/preflight":
                    stub.preflights.append(body)
                    self._reply({"deny_reasons": stub.deny})
                elif self.path == "/ip/nhi/events/ai-invocations" and stub.cut_off:
                    stub.cut_off -= 1
                    stub.cut.extend(body["events"])
                    stub.cut_event.set()
                    stub.release.wait(30)
                    self.close_connection = True
                elif self.path == "/ip/nhi/events/ai-invocations":
                    stub.pushed.extend(body["events"])
                    self._reply({})
                    stub.pushed_event.set()
                else:
                    self._reply({}, status=404)

            def _reply(self, value: object, status: int = 200) -> None:
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator[StubSlashID]:
    server = StubSlashID()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def daemons(tmp_path: Path, stub: StubSlashID) -> Iterator[Daemons]:
    env = Daemons(tmp_path, endpoint=stub.endpoint, dry_run=False)
    try:
        yield env
    finally:
        env.kill_all()


def _payload(name: str, **overrides: object) -> bytes:
    return json.dumps({**json.loads((HOOKS / name).read_text()), **overrides}).encode()


def _response_ids(path: Path) -> list[str]:
    ids = []
    for raw in path.read_bytes().splitlines():
        line = parse_line(raw)
        if line is not None and isinstance(line.payload, TokenUsageRecord):
            ids.append(line.payload.response_id)
    return ids


def test_plain_http_config_is_test_only(daemons: Daemons) -> None:
    from slashid_codex.config import CodexConfig

    with pytest.raises(ValueError, match="https"):
        CodexConfig.load(daemons.config)
    assert PlainHttpConfig.load(daemons.config).endpoint.startswith("http://")


def _rollout(daemons: Daemons) -> Path:
    day = daemons.codex_home / "sessions" / "2026" / "09" / "30"
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-09-30T15-33-36-{SESSION}.jsonl"
    shutil.copy(ROLLOUTS / "script.jsonl", rollout)
    return rollout


def _wait_pushed(stub: StubSlashID, count: int) -> None:
    give_up = time.monotonic() + 15
    while len(stub.pushed) < count and time.monotonic() < give_up:
        time.sleep(0.05)


def test_end_to_end(daemons: Daemons, stub: StubSlashID) -> None:
    rollout = _rollout(daemons)
    note = daemons.root / "note.txt"
    note.write_text("hello\n")

    info = daemons.start("-m", "tests.e2e_daemon")

    stub.deny = ["Denied by the AI hook policy."]
    ups = _payload("user_prompt_submit.json", transcript_path=str(rollout))
    out = json.loads(daemons.hook("UserPromptSubmit", ups).stdout)
    assert out == {"decision": "block", "reason": "Denied by the AI hook policy."}

    stub.deny = []
    ptu = _payload(
        "pre_tool_use_bash_script.json",
        session_id=SESSION,
        transcript_path=str(rollout),
        cwd=str(daemons.root),
        tool_input={"command": f"cat {note}"},
    )
    assert json.loads(daemons.hook("PreToolUse", ptu).stdout) == {}

    [prompt, tool] = stub.preflights
    assert prompt["request_id"] == json.loads(ups)["turn_id"]
    assert prompt["parsed_as"] == "codex-hook"
    assert prompt["identity_details"] == {"kind": "openai", "user_id": "user-abc"}
    assert tool["requested_tool_uses"][0]["tool_use_id"] == json.loads(ptu)["tool_use_id"]
    [read] = tool["accessed_files"]
    assert read["name"] == str(note)
    assert read["content_hashes"]

    stop = _payload("stop.json", transcript_path=str(rollout))
    assert json.loads(daemons.hook("Stop", stop).stdout) == {}
    assert stub.pushed_event.wait(15)
    expected = _response_ids(rollout)
    _wait_pushed(stub, len(expected))
    assert [e["request_id"] for e in stub.pushed] == expected
    assert {e["parsed_as"] for e in stub.pushed} == {"codex-rollout"}
    assert stub.auth == {f"Bearer {TOKEN}"}
    # Same daemon throughout.
    assert daemons.info() == info


def test_push_cut_off_by_exit_resent(daemons: Daemons, stub: StubSlashID) -> None:
    """The watermark was not saved, so the next daemon's trigger sends the same
    events again; the server deduplicates them by ``request_id``."""
    rollout = _rollout(daemons)
    stop = _payload("stop.json", transcript_path=str(rollout))
    expected = _response_ids(rollout)
    stub.cut_off = 1
    old = daemons.start("-m", "tests.e2e_daemon")
    assert json.loads(daemons.hook("Stop", stop).stdout) == {}
    assert stub.cut_event.wait(15)
    os.kill(old.pid, signal.SIGTERM)
    assert wait_dead(old.pid)
    assert stub.pushed == []

    daemons.start("-m", "tests.e2e_daemon")
    assert json.loads(daemons.hook("Stop", stop).stdout) == {}
    _wait_pushed(stub, len(expected))
    sent = [e["request_id"] for e in stub.pushed]
    assert sent == expected
    cut = [e["request_id"] for e in stub.cut]
    assert cut == sent[: len(cut)]


def _spawn_e2e(argv: list[str], cwd: Path, stderr_path: Path) -> subprocess.Popen[bytes]:
    """The client's spawn, of ``tests.e2e_daemon``."""
    assert argv[1:4] == ["-m", "slashid_codex", "daemon"]
    return spawn_daemon(
        [argv[0], "-m", "tests.e2e_daemon", *argv[3:]], cwd=PACKAGE, stderr_path=stderr_path
    )


def test_hook_racing_the_idle_exit(
    daemons: Daemons, stub: StubSlashID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon exits idle between a hook's ping and its event: the hook
    gets a verdict from a respawned daemon, not a broken connection, and its
    connection is never reopened."""
    daemons.write_config(endpoint=stub.endpoint, dry_run=False, daemon_idle_seconds=2)
    old = daemons.start("-m", "tests.e2e_daemon")
    raced: list[int] = []
    real_ping = cli.connect

    def ping_then_idle(info: DaemonInfo, *, deadline: float) -> DaemonConnection | None:
        conn = real_ping(info, deadline=deadline)
        if conn is not None and not raced:
            raced.append(info.pid)
            assert wait_dead(info.pid)
        return conn

    reopened: list[http.client.HTTPConnection] = []
    real_connect = http.client.HTTPConnection.connect

    def connect(self: http.client.HTTPConnection) -> None:
        if getattr(self, "opened_by_test", False):
            reopened.append(self)
        self.opened_by_test = True  # ty: ignore[unresolved-attribute]
        real_connect(self)

    monkeypatch.setattr(cli, "connect", ping_then_idle)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect)
    outs: list[str] = []
    cli.run_hook(
        "PreToolUse",
        daemons.config,
        daemons.state,
        daemons.codex_home,
        io.BytesIO(_payload("pre_tool_use_bash_sed.json")),
        spawn=_spawn_e2e,
        write=outs.append,
        exit=lambda code: pytest.fail(f"backstop exit({code})"),
    )
    assert outs == ["{}"]
    assert raced == [old.pid]
    assert "idle; exiting" in daemons.log()
    new = daemons.info()
    assert new is not None
    assert new.pid != old.pid
    assert len(stub.preflights) == 1
    assert reopened == []
