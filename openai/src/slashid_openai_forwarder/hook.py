"""A ChatGPT Work remote hook call → a SlashID preflight verdict.

The cloud orchestrator calls ``slashid_preflight`` with the arguments the
admin's ``input`` template built from the hook event (see the design note's
``requirements.toml``). The invocation is the one the Codex hook sends for
the same event, less what only a local process can see: no rollout, so no
history, and no file bytes, so files go by name.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import JsonValue
from slashid_ai_forwarder_core.content_utils import truncate_middle
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationContent,
    AIInvocationObservedV1,
    AIModel,
    AIToolUse,
    OpenAIIdentityDetails,
)
from slashid_ai_forwarder_core.normalize._base import _LenientModel
from slashid_ai_forwarder_core.normalize.normalized.connector_files import connector_refs
from slashid_ai_forwarder_core.normalize.normalized.tool_results import connector_stub
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool
from slashid_ai_forwarder_core.reads import get_files_read_by_tool

log = logging.getLogger(__name__)

PARSED_AS = "chatgpt-work-hook"
TOOL_NAME = "slashid_preflight"

Event = Literal["UserPromptSubmit", "PreToolUse"]


class HookCall(_LenientModel):
    """The tool's arguments. Only the fields the event carries arrive: a
    template naming a missing one fails the hook before it reaches us."""

    hook_event_name: Event
    session_id: str
    turn_id: str
    model: str | None = None
    prompt: str | None = None
    cwd: str | None = None
    tool_name: str | None = None
    tool_input: JsonValue = None
    tool_use_id: str | None = None


# The tool's declaration, for tools/list. ``hook_event_name`` decides which of
# the rest apply.
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hook_event_name": {"type": "string", "enum": ["UserPromptSubmit", "PreToolUse"]},
        "session_id": {"type": "string"},
        "turn_id": {"type": "string"},
        "model": {"type": "string"},
        "prompt": {"type": "string"},
        "cwd": {"type": "string"},
        "tool_name": {"type": "string"},
        "tool_input": {},
        "tool_use_id": {"type": "string"},
    },
    "required": ["hook_event_name", "session_id", "turn_id"],
}


def invocation(
    call: HookCall,
    *,
    user_id: str,
    include_raw_content: bool,
    max_content_size: int,
    now: datetime | None = None,
) -> AIInvocationObservedV1:
    """``ValueError`` when a ``PreToolUse`` names no tool: a call the policy
    cannot identify is not one to ask about as if it were a prompt."""
    timestamp = (now or datetime.now(UTC)).isoformat(timespec="milliseconds")
    common: dict[str, Any] = {
        "timestamp": timestamp.replace("+00:00", "Z"),
        "identity_details": OpenAIIdentityDetails(user_id=user_id),
        "model": AIModel(id=call.model or "unknown", provider="openai"),
        "parsed_as": PARSED_AS,
        "conversation_id": call.session_id,
    }
    if call.hook_event_name == "UserPromptSubmit":
        return AIInvocationObservedV1(
            request_id=call.turn_id,
            input=_prompt_content(call.prompt or "", include_raw_content, max_content_size),
            **common,
        )
    if not call.tool_name or not call.tool_use_id:
        raise ValueError("PreToolUse without tool_name or tool_use_id")
    tool, server = resolve_tool(call.tool_name)
    return AIInvocationObservedV1(
        request_id=f"{call.turn_id}:{call.tool_use_id}",
        requested_tool_uses=[AIToolUse(tool_id=tool.id, tool_use_id=call.tool_use_id)],
        available_tools=[tool],
        available_tool_servers=[server],
        accessed_files=_files_read(call) or None,
        **common,
    )


def _prompt_content(prompt: str, include_text: bool, max_size: int) -> AIInvocationContent:
    """The prompt as the Anthropic hook sends one: hashes always, the text
    only when the deployment opted in, which a content rule needs."""
    data = prompt.encode()
    return AIInvocationContent(
        content_hashes={
            "sha256": hashlib.sha256(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(),
        },
        mime_type="text/plain",
        byte_length=len(data),
        redacted_text=truncate_middle(prompt, max_size) if include_text else None,
    )


def _files_read(call: HookCall) -> list[AIAccessedFile]:
    """By name: the bytes are on the user's computer, out of our reach.
    A connector's file goes by its provider reference, as in Codex."""
    assert call.tool_name is not None
    paths = get_files_read_by_tool(call.tool_name, call.tool_input, call.cwd)
    files = [AIAccessedFile(name=str(p), provenance="tool_result") for p in paths]
    files += [connector_stub(cf) for cf in connector_refs(call.tool_name, call.tool_input)]
    return files


def allow() -> str:
    return "{}"


def deny(event: Event, reason: str) -> str:
    """The one JSON object Codex accepts as a block for ``event``. Its output
    structs reject unknown fields, so nothing else may ride along."""
    if event == "UserPromptSubmit":
        return json.dumps({"decision": "block", "reason": reason})
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )
