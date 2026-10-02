from __future__ import annotations

import json
from pathlib import Path

import pytest

from slashid_codex.hooks import (
    PostToolUseHook,
    PreToolUseHook,
    SessionEndHook,
    SessionStartHook,
    StopHook,
    UserPromptSubmitHook,
    parse_hook,
)

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"


def _raw(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("session_start_startup", "startup"),
        ("session_start_compact", "compact"),
        ("session_start_fork", "fork"),
        ("session_start_resume", "resume"),
    ],
)
def test_session_start(name: str, source: str) -> None:
    hook = parse_hook("SessionStart", _raw(name))
    assert isinstance(hook, SessionStartHook)
    assert hook.source == source
    assert hook.transcript_path is not None
    assert hook.transcript_path.name.endswith(f"{hook.session_id}.jsonl")


def test_user_prompt_submit() -> None:
    hook = parse_hook("UserPromptSubmit", _raw("user_prompt_submit"))
    assert isinstance(hook, UserPromptSubmitHook)
    assert hook.turn_id == "01a0f397-f1ce-7640-a04b-370d7af13c6f"
    assert hook.prompt.startswith("Run 'cat note.txt'")
    assert hook.model == "gpt-6-astra"
    assert hook.permission_mode == "bypassPermissions"
    assert hook.cwd == "/home/user/work"


def test_user_prompt_submit_attachments_keep_leading_newline() -> None:
    hook = parse_hook("UserPromptSubmit", _raw("user_prompt_submit_attachments"))
    assert isinstance(hook, UserPromptSubmitHook)
    assert hook.prompt.startswith("\n# Files mentioned by the user:\n")


def test_pre_tool_use_script_mode() -> None:
    hook = parse_hook("PreToolUse", _raw("pre_tool_use_bash_script"))
    assert isinstance(hook, PreToolUseHook)
    assert hook.tool_name == "Bash"
    assert hook.tool_input == {"command": "cat note.txt"}
    assert hook.tool_use_id.startswith("exec-")


def test_pre_tool_use_function_mode() -> None:
    hook = parse_hook("PreToolUse", _raw("pre_tool_use_bash_sed"))
    assert isinstance(hook, PreToolUseHook)
    assert hook.tool_use_id == "call_PaTVhmRLsPDp4UUH9JaOFRUl"
    assert hook.tool_input == {"command": "sed -n '1,240p' /home/user/Recipes/notes.md"}


def test_pre_tool_use_view_image() -> None:
    hook = parse_hook("PreToolUse", _raw("pre_tool_use_view_image"))
    assert isinstance(hook, PreToolUseHook)
    assert hook.tool_name == "view_image"
    assert hook.tool_input == {"path": "/home/user/Documentos/image 1.png", "detail": "high"}


def test_post_tool_use() -> None:
    bash = parse_hook("PostToolUse", _raw("post_tool_use_bash"))
    assert isinstance(bash, PostToolUseHook)
    assert bash.tool_response == "hello\n"
    image = parse_hook("PostToolUse", _raw("post_tool_use_view_image"))
    assert isinstance(image, PostToolUseHook)
    assert isinstance(image.tool_response, list)


def test_stop() -> None:
    hook = parse_hook("Stop", _raw("stop"))
    assert isinstance(hook, StopHook)
    assert hook.stop_hook_active is False
    assert hook.last_assistant_message == "hello"


def test_session_end() -> None:
    hook = parse_hook("SessionEnd", _raw("session_end"))
    assert isinstance(hook, SessionEndHook)
    assert hook.reason == "other"
    assert hook.model is None
    assert hook.permission_mode is None
    assert hook.transcript_path is not None


def test_session_end_without_transcript() -> None:
    hook = parse_hook("SessionEnd", _raw("session_end_no_transcript"))
    assert isinstance(hook, SessionEndHook)
    assert hook.transcript_path is None


def test_extra_fields_ignored() -> None:
    body = json.loads(_raw("stop"))
    body["something_new"] = {"a": 1}
    assert isinstance(parse_hook("Stop", json.dumps(body).encode()), StopHook)


def test_event_mismatch_rejected() -> None:
    with pytest.raises(ValueError):
        parse_hook("PreToolUse", _raw("user_prompt_submit"))


def test_unknown_event_rejected() -> None:
    with pytest.raises(ValueError):
        parse_hook("PreCompact", _raw("stop"))


def test_invalid_json_rejected() -> None:
    with pytest.raises(ValueError):
        parse_hook("Stop", b"{not json")
