"""Codex hook payloads, one model per measured event."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import JsonValue
from slashid_ai_forwarder_core.normalize._base import _LenientModel


class _Hook(_LenientModel):
    session_id: str
    # ``None`` on a ``SessionEnd`` for a session that never wrote a rollout.
    transcript_path: Path | None = None
    cwd: str
    # Absent on ``SessionEnd``.
    model: str | None = None
    permission_mode: str | None = None


class SessionStartHook(_Hook):
    hook_event_name: Literal["SessionStart"]
    # ``startup``, ``resume``, ``fork``, ``compact``, ``clear``.
    source: str


class UserPromptSubmitHook(_Hook):
    hook_event_name: Literal["UserPromptSubmit"]
    turn_id: str
    prompt: str


class _ToolHook(_Hook):
    turn_id: str
    tool_name: str
    tool_input: JsonValue
    # ``call_…`` in function mode, ``exec-<uuid>`` in script mode.
    tool_use_id: str


class PreToolUseHook(_ToolHook):
    hook_event_name: Literal["PreToolUse"]


# Not registered with Codex; kept so every measured payload parses.
class PostToolUseHook(_ToolHook):
    hook_event_name: Literal["PostToolUse"]
    tool_response: JsonValue = None


class StopHook(_Hook):
    hook_event_name: Literal["Stop"]
    turn_id: str
    stop_hook_active: bool = False
    last_assistant_message: str | None = None


class SessionEndHook(_Hook):
    hook_event_name: Literal["SessionEnd"]
    reason: str | None = None


HookPayload = (
    SessionStartHook
    | UserPromptSubmitHook
    | PreToolUseHook
    | PostToolUseHook
    | StopHook
    | SessionEndHook
)

_MODELS: dict[str, type[HookPayload]] = {
    "SessionStart": SessionStartHook,
    "UserPromptSubmit": UserPromptSubmitHook,
    "PreToolUse": PreToolUseHook,
    "PostToolUse": PostToolUseHook,
    "Stop": StopHook,
    "SessionEnd": SessionEndHook,
}


def parse_hook(event: str, raw: bytes) -> HookPayload:
    """``event`` is the one the hook was registered for; a payload naming
    another is rejected. Raises ``ValueError``."""
    model = _MODELS.get(event)
    if model is None:
        raise ValueError(f"unhandled hook event {event!r}")
    return model.model_validate_json(raw)
