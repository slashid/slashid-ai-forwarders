"""The steps both adapters take around a check."""

from __future__ import annotations

import time

from slashid_ai_forwarder_core.events import AIAccessedFile

from slashid_agentcore_forwarder import logs
from slashid_agentcore_forwarder.checks import Decision, decide
from slashid_agentcore_forwarder.deny import is_own_deny, single_text
from slashid_agentcore_forwarder.payloads import ToolResult, is_truncated
from slashid_agentcore_forwarder.runtime import Runtime
from slashid_agentcore_forwarder.tool_call import ToolCall, build_event


def log_if_truncated(call: ToolCall) -> None:
    """A tool input cut at the platform's size limit has lost its references."""
    if is_truncated(call.tool_input):
        logs.truncated_input(surface=call.surface, tool=call.raw_name, call_id=call.call_id)


def is_own_result(result: ToolResult, key: bytes) -> bool:
    """A result that is exactly one of our deny texts (spec 2)."""
    text = single_text(result.content, result.structuredContent)
    return text is not None and is_own_deny(text, key)


async def run_check(
    runtime: Runtime,
    call: ToolCall,
    *,
    files: list[AIAccessedFile],
    result_is_error: bool | None,
    prefix: str = "",
) -> Decision:
    """The budget starts here, after any role lookup. ``result_is_error`` None is a call check."""
    started = time.monotonic()
    event = build_event(call, result_is_error=result_is_error, files=files)
    outcome = await runtime.checker.check(event, started=started)
    check = "call" if result_is_error is None else "result"
    logs.check(call.surface, check, outcome.kind, int((time.monotonic() - started) * 1000))
    return decide(
        outcome,
        config=runtime.config,
        key=runtime.key,
        surface=call.surface,
        check=check,
        prefix=prefix,
    )
