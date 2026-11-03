"""Models of both surfaces' payloads. Tool inputs, results and everything the Gateway echoes
stay free-form JSON, held as it came (``Raw``): no field of these models validates its depth.

``McpPart`` keeps the request and the response raw, because a request that fails its envelope
is unreadable and a response that fails its envelope is a no-answer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from slashid_agentcore_forwarder.models import Lenient, NonBlank, Raw

# Harness hooks


class HarnessEventName(Lenient):
    """The event of any hook payload, whatever its type."""

    event: object = None


class HarnessContext(Lenient):
    toolName: NonBlank
    toolType: str | None = None
    toolUseId: NonBlank
    toolInput: Raw = None
    toolResult: Raw = None


class HarnessToolHook(Lenient):
    """The envelope of a ``before_tool_call`` or ``after_tool_call``; the event is read apart."""

    harnessArn: NonBlank
    sessionId: NonBlank
    hookEventId: NonBlank
    context: HarnessContext


class ToolResult(Lenient):
    """A tool result as the Gateway carries it (MCP)."""

    isError: bool | None = None
    content: Raw = None
    structuredContent: Raw = None

    @property
    def is_error(self) -> bool:
        return bool(self.isError)


class HarnessToolResult(ToolResult):
    """A Harness ``toolResult`` also says ``status``."""

    status: str | None = None

    @property
    def is_error(self) -> bool:
        return bool(self.isError) or self.status == "error"


def parse_tool_result(raw: object) -> ToolResult:
    """An empty result for anything that is not an object we can read: no references, no failure."""
    try:
        return ToolResult.model_validate(raw)
    except ValidationError:
        return ToolResult()


def parse_harness_result(raw: object) -> HarnessToolResult:
    try:
        return HarnessToolResult.model_validate(raw)
    except ValidationError:
        return HarnessToolResult()


class Truncated(Lenient):
    """What replaces a tool result (or input) over the 64 KiB hook limit."""

    truncated: Literal[True]
    preview: str | None = None


def is_truncated(raw: object) -> bool:
    try:
        Truncated.model_validate(raw)
    except ValidationError:
        return False
    return True


# Gateway interceptors


class Meta(Lenient):
    baggage: str | None = None


class RpcParams(Lenient):
    name: NonBlank
    arguments: Raw = None
    meta: Raw = Field(None, alias="_meta")

    @property
    def baggage(self) -> str | None:
        """None unless ``_meta`` is an object with a string ``baggage``."""
        try:
            return Meta.model_validate(self.meta).baggage
        except ValidationError:
            return None


class ToolsCall(Lenient):
    method: Literal["tools/call"]
    id: str | int | None = None
    params: RpcParams


class Identity(Lenient):
    awsPrincipalArn: str | None = None


class GatewayContext(Lenient):
    identity: Identity | None = None


class GatewayRequest(Lenient):
    headers: Raw = None
    body: Raw = None
    context: Raw = None


class GatewayResponse(Lenient):
    """Read ``model_fields_set`` for a ``statusCode`` that was there. A flag that is not exactly
    a boolean fails the envelope."""

    statusCode: Raw = None
    headers: Raw = None
    body: Raw
    isStreamingResponse: StrictBool | None = None


class McpPart(Lenient):
    """Read ``model_fields_set`` for a ``gatewayResponse`` that was there."""

    gatewayRequest: Raw = None
    gatewayResponse: Raw = None


class GatewayPayload(Lenient):
    interceptorInputVersion: str
    mcp: McpPart | None = None
    http: Raw = None


class ClientContext(Lenient):
    """What AWS puts in the Lambda client context of a Gateway call."""

    GATEWAY_ARN: str | None = None
    REQUEST_ID: str | None = None


def client_context(context: object) -> ClientContext:
    """A Harness hook has no client context: all fields are None."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    try:
        return ClientContext.model_validate(custom)
    except ValidationError:
        return ClientContext()


class RpcReply(Lenient):
    result: Raw = None
    error: Raw = None


# Request bodies


class _Method(Lenient):
    method: str | None = None


class Classified(BaseModel):
    """``call`` is set only for ``tools_call``."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    kind: Literal["tools_call", "other", "unreadable", "batch_with_call", "batch_other"]
    call: ToolsCall | None = None


def _is_tools_call(item: object) -> bool:
    try:
        return _Method.model_validate(item).method == "tools/call"
    except ValidationError:
        return False


def classify_body(body: object) -> Classified:
    """Tells a ``tools/call`` apart from other messages, batches and what cannot be read."""
    if isinstance(body, list):
        return Classified(
            kind="batch_with_call" if any(map(_is_tools_call, body)) else "batch_other"
        )
    try:
        method = _Method.model_validate(body).method
    except ValidationError:
        return Classified(kind="unreadable")
    if method != "tools/call":
        return Classified(kind="other")
    try:
        return Classified(kind="tools_call", call=ToolsCall.model_validate(body))
    except ValidationError:
        return Classified(kind="unreadable")
