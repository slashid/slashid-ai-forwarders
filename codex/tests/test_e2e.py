"""Client and daemon subprocesses, ``dry_run = false``, against a stub SlashID
on plain HTTP: the real httpx path for preflight and push."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from slashid_codex.rollout import TokenUsageRecord, parse_line
from tests.daemons import TOKEN, Daemons
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


def test_end_to_end(daemons: Daemons, stub: StubSlashID) -> None:
    day = daemons.codex_home / "sessions" / "2026" / "09" / "30"
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-09-30T15-33-36-{SESSION}.jsonl"
    shutil.copy(ROLLOUTS / "script.jsonl", rollout)
    note = daemons.root / "note.txt"
    note.write_text("hello\n")

    daemon = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e_daemon", *daemons.args("daemon")[3:]],
        cwd=PACKAGE,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    info = daemons.wait_info()
    assert info.pid == daemon.pid

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
    give_up = time.monotonic() + 10
    expected = _response_ids(rollout)
    while len(stub.pushed) < len(expected) and time.monotonic() < give_up:
        time.sleep(0.05)
    assert [e["request_id"] for e in stub.pushed] == expected
    assert {e["parsed_as"] for e in stub.pushed} == {"codex-rollout"}
    assert stub.auth == {f"Bearer {TOKEN}"}
    # Same daemon throughout.
    assert daemons.info() == info
