"""The Harness hook adapter: ``before_tool_call`` and ``after_tool_call`` (spec 2, 3.1, 4)."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import JsonValue, ValidationError
from slashid_ai_forwarder_core.events import AWSIdentityDetails
from slashid_ai_forwarder_core.normalize.normalized.connector_files import is_connector_tool

from slashid_agentcore_forwarder import logs
from slashid_agentcore_forwarder.checks import Decision, Outcome, decide, unreadable
from slashid_agentcore_forwarder.identity import harness_identity
from slashid_agentcore_forwarder.names import resolve_name
from slashid_agentcore_forwarder.payloads import (
    HarnessEventName,
    HarnessToolHook,
    HarnessToolResult,
    parse_harness_result,
)
from slashid_agentcore_forwarder.pipeline import is_own_result, log_if_truncated, run_check
from slashid_agentcore_forwarder.runtime import Runtime
from slashid_agentcore_forwarder.tool_call import CachedInput, ToolCall, call_files, result_files

SURFACE = "harness"
Check = Literal["call", "result"]


def _answer(decision: Decision) -> JsonValue:
    if decision.deny is None:
        return {"decision": "allow", "reason": "slashid"}
    return {"decision": "deny", "reason": decision.deny}


def allow() -> JsonValue:
    return _answer(Decision(None))


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


def cache_key(harness_arn: str, session_id: str, tool_use_id: str) -> str:
    """One text per triple: a separator inside an id cannot join two triples into one."""
    return json.dumps([harness_arn, session_id, tool_use_id])


def _key(hook: HarnessToolHook) -> str:
    return cache_key(hook.harnessArn, hook.sessionId, hook.context.toolUseId)


def no_answer(runtime: Runtime, why: logs.FailReason, check: Check) -> JsonValue:
    outcome = Outcome("no_answer", why=why)
    return _answer(
        decide(outcome, config=runtime.config, key=runtime.key, surface=SURFACE, check=check)
    )


def deny_unreadable(runtime: Runtime, reason: logs.UnreadableReason, check: Check) -> JsonValue:
    decision = unreadable(
        config=runtime.config, key=runtime.key, surface=SURFACE, check=check, reason=reason
    )
    return _answer(decision)


async def handle(runtime: Runtime, payload: dict[str, JsonValue]) -> JsonValue:
    """``payload`` is a Harness tool hook (the dispatcher keeps the other events away); any
    event but the two tool hooks is a format we cannot place and takes the no-answer path."""
    event = HarnessEventName.model_validate(payload).event
    if event not in ("before_tool_call", "after_tool_call"):
        return no_answer(runtime, "unknown_event", "call")
    check: Check = "call" if event == "before_tool_call" else "result"
    try:
        hook = HarnessToolHook.model_validate(payload)
    except ValidationError:
        if check == "call":
            return deny_unreadable(runtime, "bad_envelope", check)
        return no_answer(runtime, "error", check)
    try:
        return await (_before if check == "call" else _after)(runtime, hook)
    except RecursionError:
        return deny_unreadable(runtime, "too_deep", check)


async def _before(runtime: Runtime, hook: HarnessToolHook) -> JsonValue:
    call = _call(hook, await harness_identity(runtime.roles, hook.harnessArn))
    log_if_truncated(call)
    files = call_files(call)
    decision = await run_check(runtime, call, files=files, result_is_error=None)
    if decision.deny is None and files:
        runtime.inputs.put(
            _key(hook), CachedInput(tool_name=call.raw_name, tool_input=call.tool_input)
        )
    return _answer(decision)


def _output(result: HarnessToolResult) -> JsonValue:
    """What the connector rules read: the content list, or both fields when there is a
    ``structuredContent``."""
    if result.structuredContent is None:
        return result.content
    content = result.content if isinstance(result.content, list) else []
    return {"content": content, "structuredContent": result.structuredContent}


async def _after(runtime: Runtime, hook: HarnessToolHook) -> JsonValue:
    cached = runtime.inputs.pop(_key(hook))
    result = parse_harness_result(hook.context.toolResult)
    if is_own_result(result, runtime.key):
        return _answer(Decision(None))
    call = _call(hook, await harness_identity(runtime.roles, hook.harnessArn))
    matched = cached if cached is not None and cached.matches(call) else None
    tool_input = matched.tool_input if matched is not None else None
    if matched is None and not result.is_error:
        logs.cache_miss(
            tool=call.raw_name, call_id=call.call_id, known=is_connector_tool(call.raw_name)
        )
    files = result_files(
        call, tool_input, _output(result), config=runtime.config, skip=result.is_error
    )
    decision = await run_check(runtime, call, files=files, result_is_error=result.is_error)
    return _answer(decision)
