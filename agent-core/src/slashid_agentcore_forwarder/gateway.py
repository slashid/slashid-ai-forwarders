"""The Gateway interceptor adapter: REQUEST and RESPONSE on ``tools/call`` (spec 2, 3.1, 4).

Allow echoes the raw payload, so every decision reads the raw dict: ``GatewayPayload`` fails as a
whole on one malformed field.
"""

from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import JsonValue, ValidationError

from slashid_agentcore_forwarder import logs
from slashid_agentcore_forwarder.checks import Decision, Outcome, decide, unreadable
from slashid_agentcore_forwarder.deny import is_own_deny, single_text
from slashid_agentcore_forwarder.headers import parse_baggage, parse_headers
from slashid_agentcore_forwarder.identity import gateway_identity
from slashid_agentcore_forwarder.models import Lenient
from slashid_agentcore_forwarder.names import resolve_name
from slashid_agentcore_forwarder.payloads import (
    ClientContext,
    GatewayContext,
    ResultBody,
    RpcReply,
    ToolsCall,
    classify_body,
    is_truncated,
)
from slashid_agentcore_forwarder.runtime import Runtime
from slashid_agentcore_forwarder.tool_call import ToolCall, build_event, call_files, result_files

SURFACE = "gateway"
VERSION = "1.0"
WITHHELD = "Content withheld: "

Check = Literal["call", "result"]
RpcId = str | int | None
Mcp = dict[str, JsonValue]  # the value of an answer's ``mcp`` key


class _Mcp(Lenient):
    gatewayRequest: JsonValue = None
    gatewayResponse: JsonValue = None


class _Top(Lenient):
    mcp: _Mcp | None = None


class _Request(Lenient):
    headers: JsonValue = None
    body: JsonValue = None
    context: JsonValue = None


class _Response(Lenient):
    statusCode: JsonValue = 200
    headers: JsonValue = None
    body: JsonValue = None
    isStreamingResponse: bool = False


class _RpcId(Lenient):
    id: JsonValue = None


class _Result(ResultBody):
    content: JsonValue = None
    structuredContent: JsonValue = None


def _out(**answer: JsonValue) -> JsonValue:
    return {"interceptorOutputVersion": VERSION, **answer}


def _rpc_id(body: JsonValue) -> RpcId:
    """The JSON-RPC id of a message; ``None`` for a batch, an absent or an unreadable one."""
    try:
        value = _RpcId.model_validate(body).id
    except ValidationError:
        return None
    return value if isinstance(value, str | int) and not isinstance(value, bool) else None


def _answer(decision: Decision, *, rpc_id: RpcId, echo: Mcp) -> JsonValue:
    """``echo`` is the ``mcp`` value that gives the payload back unchanged."""
    if decision.deny is None:
        return _out(mcp=echo)
    block: JsonValue = {"type": "text", "text": decision.deny}
    body: JsonValue = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {"isError": True, "content": [block]},
    }
    return _out(mcp={"transformedGatewayResponse": {"statusCode": 200, "body": body}})


def _echo_request(request: _Request) -> Mcp:
    return {"transformedGatewayRequest": {"body": request.body}}


def _echo_response(response: _Response) -> Mcp:
    echoed: dict[str, JsonValue] = {"statusCode": response.statusCode}
    if response.headers is not None:
        echoed["headers"] = response.headers
    echoed["body"] = response.body
    return {"transformedGatewayResponse": echoed}


def _prefix(check: Check) -> str:
    return WITHHELD if check == "result" else ""


def _no_answer(
    runtime: Runtime, why: logs.FailReason, *, check: Check, rpc_id: RpcId, echo: Mcp
) -> JsonValue:
    decision = decide(
        Outcome("no_answer", why=why),
        config=runtime.config,
        key=runtime.key,
        surface=SURFACE,
        check=check,
        prefix=_prefix(check),
    )
    return _answer(decision, rpc_id=rpc_id, echo=echo)


def _unreadable(
    runtime: Runtime, reason: logs.UnreadableReason, *, check: Check, rpc_id: RpcId, echo: Mcp
) -> JsonValue:
    decision = unreadable(
        config=runtime.config,
        key=runtime.key,
        surface=SURFACE,
        check=check,
        reason=reason,
        prefix=_prefix(check),
    )
    return _answer(decision, rpc_id=rpc_id, echo=echo)


def _not_a_call(runtime: Runtime, request: _Request, *, check: Check, echo: Mcp) -> JsonValue:
    """The answer for a body that is not a readable ``tools/call``: other messages and batches
    without a call are echoed, the rest is unreadable."""
    kind = classify_body(request.body).kind
    if kind in ("other", "batch_other"):
        return _out(mcp=echo)
    reason: logs.UnreadableReason = (
        "batch_with_call" if kind == "batch_with_call" else "bad_tool_call"
    )
    return _unreadable(runtime, reason, check=check, rpc_id=_rpc_id(request.body), echo=echo)


def _principal(raw: JsonValue) -> str | None:
    try:
        identity = GatewayContext.model_validate(raw).identity
    except ValidationError:
        return None
    return None if identity is None else identity.awsPrincipalArn


async def _call(
    runtime: Runtime, ctx: ClientContext, request: _Request, call: ToolsCall, check: Check
) -> ToolCall | None:
    """The call as the Gateway saw it; ``None`` with neither a Gateway ARN nor a principal.

    A request with no ``context`` (a JWT Gateway) takes its principal from the Gateway role. A
    RESPONSE with no ``context`` is treated the same way, so its principal can differ from the
    call check's.
    """
    headers = parse_headers(request.headers)
    baggage = parse_baggage(headers.baggage).merged(parse_baggage(call.params.baggage))
    identity = await gateway_identity(
        runtime.roles,
        gateway_arn=ctx.GATEWAY_ARN or None,
        principal=_principal(request.context),
        baggage=baggage,
    )
    if identity is None:
        return None
    call_id = ctx.REQUEST_ID
    if not call_id:
        call_id = str(uuid.uuid4())
        logs.missing_request_id()
    scope = f"{baggage.session_id}:" if baggage.session_id else ""
    suffix = "request" if check == "call" else "result"
    return ToolCall(
        surface=SURFACE,
        raw_name=call.params.name,
        resolved_name=resolve_name(call.params.name, None),
        call_id=call_id,
        tool_input=call.params.arguments,
        request_id=f"{scope}{call_id}:{suffix}",
        conversation_id=baggage.session_id,
        user_agent=headers.user_agent,
        identity=identity,
    )


async def handle(runtime: Runtime, payload: dict[str, JsonValue], ctx: ClientContext) -> JsonValue:
    """``payload`` is a Gateway interceptor payload (the dispatcher keeps the others away); a
    ``gatewayResponse`` that is absent counts as null, a REQUEST."""
    try:
        mcp = _Top.model_validate(payload).mcp
    except ValidationError:
        return _no_answer(runtime, "error", check="call", rpc_id=None, echo={})
    if mcp is None:
        return _out(http={})
    if mcp.gatewayResponse is None:
        return await _before(runtime, mcp.gatewayRequest, ctx)
    return await _after(runtime, mcp.gatewayResponse, mcp.gatewayRequest, ctx)


async def _before(runtime: Runtime, raw_request: JsonValue, ctx: ClientContext) -> JsonValue:
    try:
        request = _Request.model_validate(raw_request)
    except ValidationError:
        return _unreadable(runtime, "bad_envelope", check="call", rpc_id=None, echo={})
    echo = _echo_request(request)
    parsed = classify_body(request.body).call
    if parsed is None:
        return _not_a_call(runtime, request, check="call", echo=echo)
    rpc_id = _rpc_id(request.body)
    call = await _call(runtime, ctx, request, parsed, "call")
    if call is None:
        return _no_answer(runtime, "no_identity", check="call", rpc_id=rpc_id, echo=echo)
    if is_truncated(call.tool_input):
        logs.truncated_input(surface=SURFACE, tool=call.raw_name, call_id=call.call_id)
    started = time.monotonic()
    event = build_event(call, result_is_error=None, files=call_files(call))
    outcome = await runtime.checker.check(event, started=started)
    decision = decide(
        outcome, config=runtime.config, key=runtime.key, surface=SURFACE, check="call"
    )
    return _answer(decision, rpc_id=rpc_id, echo=echo)


def _reply(body: JsonValue) -> RpcReply:
    try:
        return RpcReply.model_validate(body)
    except ValidationError:
        return RpcReply()


def _result(reply: RpcReply) -> _Result:
    """An ``error`` first; a ``result`` is read only when it is an object."""
    if reply.error is not None:
        return _Result(isError=True)
    try:
        return _Result.model_validate(reply.result)
    except ValidationError:
        return _Result()


async def _after(
    runtime: Runtime, raw_response: JsonValue, raw_request: JsonValue, ctx: ClientContext
) -> JsonValue:
    try:
        response = _Response.model_validate(raw_response)
    except ValidationError:
        return _no_answer(runtime, "error", check="result", rpc_id=None, echo={})
    echo = _echo_response(response)
    if response.isStreamingResponse:
        logs.pass_through(surface=SURFACE, reason="streaming_response")
        return _out(mcp=echo)
    reply = _reply(response.body)
    result = _result(reply)
    text = single_text(result.content, result.structuredContent)
    if text is not None and is_own_deny(text, runtime.key):
        return _out(mcp=echo)
    if raw_request is None:  # no tool name to check
        rpc_id = _rpc_id(response.body)
        return _no_answer(runtime, "no_request", check="result", rpc_id=rpc_id, echo=echo)
    try:
        request = _Request.model_validate(raw_request)
    except ValidationError:
        rpc_id = _rpc_id(response.body)
        return _no_answer(runtime, "error", check="result", rpc_id=rpc_id, echo=echo)
    parsed = classify_body(request.body).call
    if parsed is None:
        return _not_a_call(runtime, request, check="result", echo=echo)
    rpc_id = _rpc_id(request.body)
    call = await _call(runtime, ctx, request, parsed, "result")
    if call is None:
        return _no_answer(runtime, "no_identity", check="result", rpc_id=rpc_id, echo=echo)
    started = time.monotonic()
    files = []
    if not result.isError and isinstance(reply.result, dict):
        files = result_files(call, call.tool_input, reply.result, config=runtime.config)
    event = build_event(call, result_is_error=result.isError, files=files)
    outcome = await runtime.checker.check(event, started=started)
    decision = decide(
        outcome,
        config=runtime.config,
        key=runtime.key,
        surface=SURFACE,
        check="result",
        prefix=WITHHELD,
    )
    return _answer(decision, rpc_id=rpc_id, echo=echo)
