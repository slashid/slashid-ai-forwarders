"""The Harness hook adapter: ``before_tool_call`` and ``after_tool_call`` (spec 2, 3.1, 4)."""

from __future__ import annotations

import time

from pydantic import JsonValue, ValidationError
from slashid_ai_forwarder_core.events import AWSIdentityDetails
from slashid_ai_forwarder_core.normalize.normalized.connector_files import is_connector_tool

from slashid_agentcore_forwarder import logs
from slashid_agentcore_forwarder.checks import Decision, Outcome, decide, unreadable
from slashid_agentcore_forwarder.deny import is_own_deny, single_text
from slashid_agentcore_forwarder.identity import harness_identity
from slashid_agentcore_forwarder.models import Lenient
from slashid_agentcore_forwarder.names import resolve_name
from slashid_agentcore_forwarder.payloads import (
    HarnessToolHook,
    ToolResult,
    is_truncated,
    parse_tool_result,
)
from slashid_agentcore_forwarder.runtime import Runtime
from slashid_agentcore_forwarder.tool_call import (
    CachedInput,
    ToolCall,
    build_event,
    call_files,
    result_files,
)

SURFACE = "harness"


def _answer(decision: Decision) -> JsonValue:
    if decision.deny is None:
        return {"decision": "allow", "reason": "slashid"}
    return {"decision": "deny", "reason": decision.deny}


def _call(hook: HarnessToolHook, identity: AWSIdentityDetails) -> ToolCall:
    ctx = hook.context
    return ToolCall(
        surface=SURFACE,
        raw_name=ctx.toolName,
        resolved_name=resolve_name(ctx.toolName, ctx.toolType),
        call_id=ctx.toolUseId,
        tool_input=ctx.toolInput,
        request_id=f"{hook.sessionId}:{hook.hookEventId}",
        conversation_id=hook.sessionId,
        user_agent=None,
        identity=identity,
    )


def _cache_key(hook: HarnessToolHook) -> str:
    return f"{hook.sessionId}:{hook.context.toolUseId}"


class _Event(Lenient):
    event: str | None = None


async def handle(runtime: Runtime, payload: dict[str, JsonValue]) -> JsonValue:
    """``payload`` is a Harness tool hook (the dispatcher keeps the other events away)."""
    if _Event.model_validate(payload).event == "before_tool_call":
        return await _before(runtime, payload)
    return await _after(runtime, payload)


async def _before(runtime: Runtime, payload: dict[str, JsonValue]) -> JsonValue:
    config, key = runtime.config, runtime.key
    try:
        hook = HarnessToolHook.model_validate(payload)
    except ValidationError:
        return _answer(
            unreadable(config=config, key=key, surface=SURFACE, check="call", reason="bad_envelope")
        )
    call = _call(hook, await harness_identity(runtime.roles, hook.harnessArn))
    if is_truncated(call.tool_input):
        logs.truncated_input(surface=SURFACE, tool=call.raw_name, call_id=call.call_id)
    started = time.monotonic()
    files = call_files(call)
    event = build_event(call, result_is_error=None, files=files)
    outcome = await runtime.checker.check(event, started=started)
    decision = decide(outcome, config=config, key=key, surface=SURFACE, check="call")
    if decision.deny is None and files:
        runtime.inputs.put(
            _cache_key(hook), CachedInput(tool_name=call.raw_name, tool_input=call.tool_input)
        )
    return _answer(decision)


def _output(result: ToolResult) -> JsonValue:
    """What the connector rules read: the content list, or both fields when there is a
    ``structuredContent``."""
    if result.structuredContent is None:
        return result.content
    content = result.content if isinstance(result.content, list) else []
    return {"content": content, "structuredContent": result.structuredContent}


async def _after(runtime: Runtime, payload: dict[str, JsonValue]) -> JsonValue:
    config, key = runtime.config, runtime.key
    try:
        hook = HarnessToolHook.model_validate(payload)
    except ValidationError:
        outcome = Outcome("no_answer", why="error")
        return _answer(decide(outcome, config=config, key=key, surface=SURFACE, check="result"))
    cached = runtime.inputs.pop(_cache_key(hook))
    result = parse_tool_result(hook.context.toolResult)
    text = single_text(result.content, result.structuredContent)
    if text is not None and is_own_deny(text, key):
        return _answer(Decision(None))
    call = _call(hook, await harness_identity(runtime.roles, hook.harnessArn))
    matched = cached if cached is not None and cached.matches(call) else None
    tool_input = matched.tool_input if matched is not None else None
    if matched is None and not result.is_error:
        logs.cache_miss(
            tool=call.raw_name, call_id=call.call_id, known=is_connector_tool(call.raw_name)
        )
    files = (
        [] if result.is_error else result_files(call, tool_input, _output(result), config=config)
    )
    started = time.monotonic()
    event = build_event(call, result_is_error=result.is_error, files=files)
    outcome = await runtime.checker.check(event, started=started)
    return _answer(decide(outcome, config=config, key=key, surface=SURFACE, check="result"))
