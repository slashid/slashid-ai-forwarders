from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.files import hash_local_file
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool
from slashid_ai_forwarder_core.sink import PreflightError

from slashid_codex import preflight as preflight_module
from slashid_codex.cache import SessionCache
from slashid_codex.config import CodexConfig
from slashid_codex.hooks import PreToolUseHook, UserPromptSubmitHook
from slashid_codex.preflight import Preflight, Verdict, fail_verdict
from slashid_codex.state import RecordStoreBusy, SqliteFileRecordStore, connect

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
INTERRUPT_SESSION = "01a0f38b-f3a4-7c70-95e2-420a7fcbcc03"
SESSION = "sess-1"
TURN = "turn-1"
BASH = resolve_tool("Bash")


class FakeSink:
    def __init__(self, reasons: list[str] | None = None, error: Exception | None = None) -> None:
        self.reasons = reasons or []
        self.error = error
        self.invocations: list[AIInvocationObservedV1] = []

    async def preflight(self, invocation: AIInvocationObservedV1, *, deadline: float) -> list[str]:
        self.invocations.append(invocation)
        if self.error is not None:
            raise self.error
        return self.reasons


class Env:
    def __init__(self, tmp_path: Path, config: CodexConfig, sink: FakeSink) -> None:
        self.tmp_path = tmp_path
        self.config = config
        self.sink = sink
        self.store = SqliteFileRecordStore(lambda: connect(tmp_path / "state"))
        self.cache = SessionCache(codex_home=config.codex_home)
        self.preflight = Preflight(config, sink, self.store, self.cache)

    @property
    def sent(self) -> AIInvocationObservedV1:
        [invocation] = self.sink.invocations
        return invocation


@pytest.fixture
def env(tmp_path: Path, make_config: Callable[..., CodexConfig]) -> Env:
    return Env(tmp_path, make_config(), FakeSink())


def _prompt(*paths: Path, image: Path | None = None) -> str:
    entries = [f"## {p.name}: {p}" for p in paths]
    if image is not None:
        entries.append(f"## {image.name}: {image}\nImage attachment: true")
    body = "\n\n".join(entries)
    return f"\n# Files mentioned by the user:\n\n{body}\n\n## My request:\nhi\n"


def _ups(prompt: str, *, transcript: Path | None = None, session_id: str = SESSION):
    return UserPromptSubmitHook.model_validate(
        {
            "session_id": session_id,
            "turn_id": TURN,
            "transcript_path": str(transcript) if transcript else None,
            "cwd": "/somewhere",
            "hook_event_name": "UserPromptSubmit",
            "model": "gpt-5.5",
            "prompt": prompt,
        }
    )


def _ptu(command: str, *, cwd: Path, transcript: Path | None = None, tool_use_id: str = "call_1"):
    return PreToolUseHook.model_validate(
        {
            "session_id": SESSION,
            "turn_id": TURN,
            "transcript_path": str(transcript) if transcript else None,
            "cwd": str(cwd),
            "hook_event_name": "PreToolUse",
            "model": "gpt-5.5",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_use_id": tool_use_id,
        }
    )


def _rollout(path: Path, *payloads: tuple[str, dict[str, object]]) -> Path:
    lines = [
        json.dumps({"timestamp": "2026-09-30T21:53:55.017Z", "type": kind, "payload": payload})
        for kind, payload in payloads
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
# UserPromptSubmit
# --------------------------------------------------------------------------


async def test_prompt_attachments(env: Env) -> None:
    present = env.tmp_path / "a report.txt"
    present.write_text("hello")
    missing = env.tmp_path / "gone.pdf"
    verdict = await env.preflight.user_prompt_submit(_ups(_prompt(present, missing)))

    assert verdict == Verdict()
    sent = env.sent
    assert sent.request_id == TURN
    assert sent.parsed_as == "codex-hook"
    assert sent.conversation_id == SESSION
    assert sent.identity_details.model_dump() == {
        "kind": "openai",
        "user_id": "user-abc",
        "service_account_id": None,
        "api_key_id": None,
        "api_key_hash": None,
    }
    assert sent.model.id == "gpt-5.5"
    assert sent.model.provider == "openai"
    assert sent.used_tools is None
    assert sent.available_tools is None
    assert sent.accessed_files is not None
    first, second = sent.accessed_files
    assert first.name == str(present)
    assert first.provenance == "attachment"
    assert first.content_hashes is not None
    assert first.byte_length == 5
    assert second.name == str(missing)
    assert second.content_hashes is None
    assert env.store.for_round(SESSION, [TURN], []) == sent.accessed_files


async def test_prompt_without_attachments(env: Env) -> None:
    await env.preflight.user_prompt_submit(_ups("just text"))
    assert env.sent.accessed_files is None
    assert env.store.for_round(SESSION, [TURN], []) == []


async def test_prompt_after_interrupt(env: Env) -> None:
    raw = (ROLLOUTS / "interrupt.jsonl").read_text().splitlines(keepends=True)[:70]
    raw[61] = raw[61].replace('"exit_code":0', '"exit_code":1')
    transcript = env.tmp_path / "rollout.jsonl"
    transcript.write_text("".join(raw))
    read = env.tmp_path / "read.txt"
    read.write_text("x")
    entry = hash_local_file(read, max_bytes=100, provenance="tool_result")
    env.store.put_call(INTERRUPT_SESSION, "t0", "call_8SuKXzdmQRKmwFyUjfSrl5k0", entry)

    await env.preflight.user_prompt_submit(
        _ups("go on", transcript=transcript, session_id=INTERRUPT_SESSION)
    )

    sent = env.sent
    assert sent.used_tools is not None
    assert [(u.tool_use_id, u.is_error, u.tool_id) for u in sent.used_tools] == [
        ("call_8SuKXzdmQRKmwFyUjfSrl5k0", False, BASH[0].id),
        ("call_bVjUg0JaP7bAjS7DabNzw9Ws", False, BASH[0].id),
        ("call_bnrIonlcAh2ZHUsa5NPnwq9Y", True, BASH[0].id),
        ("call_YTB0L3FnNZZmp1ad85aDsKsv", False, BASH[0].id),
    ]
    assert sent.available_tools == [BASH[0]]
    assert sent.available_tool_servers == [BASH[1]]
    assert sent.accessed_files == [entry]
    # Released for eviction once the head is read.
    assert [s.in_use for s in env.cache._sessions.values()] == [0]


async def test_record_store_busy_still_answers(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*_: object) -> None:
        raise RecordStoreBusy("locked")

    monkeypatch.setattr(env.store, "put_turn", busy)
    present = env.tmp_path / "a.txt"
    present.write_text("a")
    env.sink.reasons = ["no"]
    verdict = await env.preflight.user_prompt_submit(_ups(_prompt(present)))
    assert verdict == Verdict(decision="block", reason="no")


# --------------------------------------------------------------------------
# PreToolUse
# --------------------------------------------------------------------------


async def test_tool_read_uses_pending_workdir(env: Env) -> None:
    workdir = env.tmp_path / "w"
    workdir.mkdir()
    (workdir / "notes.md").write_text("notes")
    command = "sed -n '1,240p' notes.md"
    transcript = _rollout(
        env.tmp_path / "rollout.jsonl",
        ("session_meta", {"id": SESSION}),
        ("turn_context", {"turn_id": TURN, "cwd": "/elsewhere", "model": "gpt-5.5"}),
        (
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": command, "workdir": str(workdir)}),
                "call_id": "call_1",
            },
        ),
    )
    await env.preflight.pre_tool_use(
        _ptu(command, cwd=env.tmp_path / "elsewhere", transcript=transcript)
    )

    sent = env.sent
    assert sent.request_id == f"{TURN}:call_1"
    assert sent.requested_tool_uses is not None
    [use] = sent.requested_tool_uses
    assert use.tool_id == BASH[0].id
    assert use.tool_use_id == "call_1"
    assert use.is_error is None
    assert sent.available_tools == [BASH[0]]
    assert sent.available_tool_servers == [BASH[1]]
    assert sent.used_tools is None
    assert sent.accessed_files is not None
    [entry] = sent.accessed_files
    assert entry.name == str(workdir / "notes.md")
    assert entry.provenance == "tool_result"
    assert entry.content_hashes is not None
    assert env.store.for_round(SESSION, [], ["call_1"]) == [entry]


async def test_tool_read_script_mode_uses_payload_cwd(env: Env) -> None:
    cwd = env.tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "note.txt").write_text("n")
    await env.preflight.pre_tool_use(_ptu("cat note.txt", cwd=cwd, tool_use_id="exec-1"))
    assert env.sent.accessed_files is not None
    [entry] = env.sent.accessed_files
    assert entry.name == str(cwd / "note.txt")
    assert env.store.for_round(SESSION, [], ["exec-1"]) == [entry]


async def test_tool_read_expands_home(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    home = env.tmp_path / "home"
    home.mkdir()
    (home / "notes.md").write_text("mine")
    monkeypatch.setenv("HOME", str(home))
    await env.preflight.pre_tool_use(_ptu("cat ~/notes.md", cwd=env.tmp_path))
    assert env.sent.accessed_files is not None
    [entry] = env.sent.accessed_files
    assert entry.name == str(home / "notes.md")
    assert entry.content_hashes is not None


async def test_tool_without_read(env: Env) -> None:
    await env.preflight.pre_tool_use(_ptu("ls -la", cwd=env.tmp_path))
    assert env.sent.accessed_files is None
    assert env.store.for_round(SESSION, [], ["call_1"]) == []


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


async def test_deny_reasons_block(env: Env) -> None:
    env.sink.reasons = ["a", "b"]
    verdict = await env.preflight.pre_tool_use(_ptu("ls", cwd=env.tmp_path))
    assert verdict.model_dump(exclude_none=True) == {"decision": "block", "reason": "a b"}


async def test_allow_is_empty(env: Env) -> None:
    verdict = await env.preflight.pre_tool_use(_ptu("ls", cwd=env.tmp_path))
    assert verdict.model_dump(exclude_none=True) == {}


@pytest.mark.parametrize("mode", ["deny", "allow"])
async def test_preflight_error_fail_mode(
    tmp_path: Path, make_config: Callable[..., CodexConfig], mode: str
) -> None:
    env = Env(
        tmp_path, make_config(verdict_fail_mode=mode), FakeSink(error=PreflightError("HTTP 503"))
    )
    verdict = await env.preflight.pre_tool_use(_ptu("ls", cwd=tmp_path))
    if mode == "allow":
        assert verdict.model_dump(exclude_none=True) == {}
    else:
        assert verdict.decision == "block"
        assert verdict.reason is not None
        assert "HTTP 503" in verdict.reason


def test_fail_verdict(make_config: Callable[..., CodexConfig]) -> None:
    deny = fail_verdict(make_config(), "Failed to start the SlashID Codex daemon.")
    assert deny == Verdict(decision="block", reason="Failed to start the SlashID Codex daemon.")
    assert fail_verdict(make_config(verdict_fail_mode="allow"), "x") == Verdict()


# --------------------------------------------------------------------------
# Hashing caps
# --------------------------------------------------------------------------


async def test_file_count_cap(env: Env) -> None:
    paths = []
    for index in range(51):
        path = env.tmp_path / f"f{index}.txt"
        path.write_text("x")
        paths.append(path)
    await env.preflight.user_prompt_submit(_ups(_prompt(*paths)))
    files = env.sent.accessed_files
    assert files is not None
    assert [f.content_hashes is not None for f in files] == [True] * 50 + [False]


async def test_total_bytes_cap(
    tmp_path: Path, make_config: Callable[..., CodexConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight_module, "MAX_TOTAL_BYTES", 10)
    env = Env(tmp_path, make_config(max_file_bytes=5), FakeSink())
    sizes = [4, 6, 4, 4, 1]
    paths = []
    for index, size in enumerate(sizes):
        path = tmp_path / f"f{index}.txt"
        path.write_text("x" * size)
        paths.append(path)
    await env.preflight.user_prompt_submit(_ups(_prompt(*paths)))
    files = env.sent.accessed_files
    assert files is not None
    # 6 > max_file_bytes; the third 4 would pass 10 in total.
    assert [f.content_hashes is not None for f in files] == [True, False, True, False, True]


# --------------------------------------------------------------------------
# Deadline from hook arrival
# --------------------------------------------------------------------------


async def test_deadline_passed_to_sink(env: Env) -> None:
    deadlines: list[float] = []

    async def preflight(invocation: AIInvocationObservedV1, *, deadline: float) -> list[str]:
        deadlines.append(deadline)
        return []

    env.sink.preflight = preflight  # ty: ignore[invalid-assignment]
    deadline = time.monotonic() + 5
    await env.preflight.pre_tool_use(_ptu("ls", cwd=env.tmp_path), deadline=deadline)
    assert deadlines == [deadline]


async def test_slow_preparation_fails_at_deadline(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(*_: object) -> None:
        time.sleep(0.5)

    monkeypatch.setattr(env.preflight, "_workdir", slow)
    start = time.monotonic()
    verdict = await env.preflight.pre_tool_use(_ptu("ls", cwd=env.tmp_path), deadline=start + 0.1)
    assert time.monotonic() - start < 0.4
    assert verdict.decision == "block"
    assert verdict.reason is not None
    assert "deadline" in verdict.reason
    assert env.sink.invocations == []


async def test_preparation_on_its_own_bounded_pool(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preparation stuck on a dead mount neither starves the default executor
    nor keeps the process from exiting."""
    release = threading.Event()

    def stuck(*_: object) -> None:
        release.wait(10)

    monkeypatch.setattr(env.preflight, "_workdir", stuck)
    before = {t for t in threading.enumerate() if t.name.startswith("codex-preflight")}
    try:
        start = time.monotonic()
        verdicts = await asyncio.gather(
            *(
                env.preflight.pre_tool_use(_ptu("ls", cwd=env.tmp_path), deadline=start + 0.2)
                for _ in range(40)
            )
        )
        assert {v.decision for v in verdicts} == {"block"}
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 1), 2) == 1
        pool = {t for t in threading.enumerate() if t.name.startswith("codex-preflight")} - before
        assert len(pool) == preflight_module.PREPARE_WORKERS
        assert all(t.daemon for t in pool)
    finally:
        release.set()


async def test_hashing_stops_past_half_the_budget(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    env.preflight = Preflight(env.config, env.sink, env.store, env.cache, monotonic=lambda: now[0])
    real = preflight_module.hash_local_file

    def hash_and_tick(*args: Any, **kwargs: Any) -> Any:
        now[0] += 1.5
        return real(*args, **kwargs)

    monkeypatch.setattr(preflight_module, "hash_local_file", hash_and_tick)
    paths = []
    for index in range(4):
        path = env.tmp_path / f"f{index}.txt"
        path.write_text("x")
        paths.append(path)
    # Budget 8 s: hashing stops once 4 s are spent.
    await env.preflight.user_prompt_submit(_ups(_prompt(*paths)), deadline=108.0)
    files = env.sent.accessed_files
    assert files is not None
    assert [f.content_hashes is not None for f in files] == [True, True, True, False]
