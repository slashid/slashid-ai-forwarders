"""Models of both surfaces' payloads. Tool inputs and results stay free-form JSON."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from slashid_agentcore_forwarder.models import Lenient

# Harness hooks


class HarnessContext(Lenient):
    toolName: str = Field(min_length=1)
    toolType: str | None = None
    toolUseId: str = Field(min_length=1)
    toolInput: JsonValue = None
    toolResult: JsonValue = None


class HarnessToolHook(Lenient):
    event: Literal["before_tool_call", "after_tool_call"]
    harnessArn: str = Field(min_length=1)
    sessionId: str = Field(min_length=1)
    hookEventId: str = Field(min_length=1)
    context: HarnessContext


class ToolResult(Lenient):
    status: str | None = None
    isError: bool | None = None
    content: JsonValue = None
    structuredContent: JsonValue = None

    @property
    def is_error(self) -> bool:
        return bool(self.isError) or self.status == "error"


def parse_tool_result(raw: JsonValue) -> ToolResult:
    """An empty result for anything that is not an object we can read: no references, no failure."""
    try:
        return ToolResult.model_validate(raw)
    except ValidationError:
        return ToolResult()


class Truncated(Lenient):
    """What replaces a tool result (or input) over the 64 KiB hook limit."""

    truncated: Literal[True]
    preview: str | None = None


def is_truncated(raw: JsonValue) -> bool:
    try:
        Truncated.model_validate(raw)
    except ValidationError:
        return False
    return True


# Gateway interceptors


class Meta(Lenient):
    baggage: str | None = None


class RpcParams(Lenient):
    name: str = Field(min_length=1)
    arguments: JsonValue = None
    meta: Meta | None = Field(None, alias="_meta")


class ToolsCall(Lenient):
    method: Literal["tools/call"]
    id: str | int | None = None
    params: RpcParams


class Identity(Lenient):
    awsPrincipalArn: str | None = None


class GatewayContext(Lenient):
    identity: Identity | None = None


class GatewayRequest(Lenient):
    headers: JsonValue = None
    body: JsonValue
    context: GatewayContext | None = None


class GatewayResponse(Lenient):
    statusCode: int = 200
    headers: JsonValue = None
    body: JsonValue = None
    isStreamingResponse: bool = False


class McpPart(Lenient):
    gatewayRequest: GatewayRequest | None = None
    gatewayResponse: GatewayResponse | None = None


class GatewayPayload(Lenient):
    interceptorInputVersion: str
    mcp: McpPart | None = None
    http: JsonValue = None


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
    result: JsonValue = None
    error: JsonValue = None


class ResultBody(Lenient):
    isError: bool = False


# Request bodies


class _Method(Lenient):
    method: str | None = None


class Classified(BaseModel):
    """``call`` is set only for ``tools_call``."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["tools_call", "other", "unreadable", "batch_with_call", "batch_other"]
    call: ToolsCall | None = None


def _is_tools_call(item: JsonValue) -> bool:
    try:
        return _Method.model_validate(item).method == "tools/call"
    except ValidationError:
        return False


def classify_body(body: JsonValue) -> Classified:
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
