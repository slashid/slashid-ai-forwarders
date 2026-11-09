from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from slashid_openai_forwarder import hook
from slashid_openai_forwarder.hook import HookCall

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _prompt(**kw: object) -> HookCall:
    return HookCall.model_validate(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "turn_id": "t1",
            "model": "gpt-5.5",
            "prompt": "summarize the Q3 numbers",
            **kw,
        }
    )


def _tool(**kw: object) -> HookCall:
    return HookCall.model_validate(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "turn_id": "t1",
            "cwd": "/Users/alice/repo",
            "tool_name": "Bash",
            "tool_input": {"command": "cat secrets/plan.xlsx"},
            "tool_use_id": "call_1",
            **kw,
        }
    )


def test_a_prompt_carries_its_text_only_when_raw_content_is_on() -> None:
    off = hook.invocation(
        _prompt(), user_id="user-1", include_raw_content=False, max_content_size=100, now=NOW
    )
    on = hook.invocation(
        _prompt(), user_id="user-1", include_raw_content=True, max_content_size=100, now=NOW
    )
    assert off.input is not None and off.input.redacted_text is None
    assert off.input.content_hashes is not None and "sha256" in off.input.content_hashes
    assert on.input is not None and on.input.redacted_text == "summarize the Q3 numbers"
    assert on.request_id == "t1"
    assert on.conversation_id == "s1"
    assert on.timestamp == "2026-11-01T12:00:00.000Z"
    assert on.identity_details.model_dump(exclude_none=True) == {
        "kind": "openai",
        "user_id": "user-1",
    }
    assert on.requested_tool_uses is None


def test_a_tool_call_is_requested_and_names_the_files_it_reads() -> None:
    inv = hook.invocation(
        _tool(), user_id="user-1", include_raw_content=False, max_content_size=100, now=NOW
    )
    assert inv.request_id == "t1:call_1"
    assert inv.requested_tool_uses is not None and len(inv.requested_tool_uses) == 1
    assert inv.available_tools is not None
    assert inv.requested_tool_uses[0].tool_id == inv.available_tools[0].id
    assert inv.accessed_files is not None
    assert [(f.name, f.content_hashes) for f in inv.accessed_files] == [("plan.xlsx", None)]
    # No model in the template: the server still needs one.
    assert inv.model.id == "unknown"


def test_a_tool_call_without_a_tool_is_not_judged_as_a_prompt() -> None:
    with pytest.raises(ValueError):
        hook.invocation(
            _tool(tool_name=None),
            user_id="user-1",
            include_raw_content=False,
            max_content_size=100,
        )


def test_the_deny_shapes_are_the_ones_codex_accepts() -> None:
    assert json.loads(hook.deny("UserPromptSubmit", "no")) == {"decision": "block", "reason": "no"}
    assert json.loads(hook.deny("PreToolUse", "no")) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "no",
        }
    }
    assert json.loads(hook.allow()) == {}
