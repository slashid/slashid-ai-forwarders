"""Pydantic schemas for the OpenAI Responses API wire shapes we consume."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from ..._base import _LenientModel

# --------------------------------------------------------------------------
# Content parts
# --------------------------------------------------------------------------


class ResponsesInputText(_LenientModel):
    type: Literal["input_text"]
    text: str


class ResponsesOutputText(_LenientModel):
    type: Literal["output_text"]
    text: str


class ResponsesInputImage(_LenientModel):
    type: Literal["input_image"]
    image_url: str | None = None
    detail: str | None = None


class ResponsesUnknownPart(_LenientModel):
    type: str


ResponsesPart = Annotated[
    ResponsesInputText | ResponsesOutputText | ResponsesInputImage | ResponsesUnknownPart,
    Field(union_mode="left_to_right"),
]

# --------------------------------------------------------------------------
# Items (request input and response output)
# --------------------------------------------------------------------------


class ResponsesMessage(_LenientModel):
    # Easy-input messages omit ``type``.
    type: Literal["message"] = "message"
    role: Literal["user", "assistant", "system", "developer"]
    content: str | list[ResponsesPart]
    phase: str | None = None


class ResponsesFunctionCall(_LenientModel):
    type: Literal["function_call"]
    call_id: str
    name: str
    arguments: str


class ResponsesFunctionCallOutput(_LenientModel):
    type: Literal["function_call_output"]
    call_id: str
    output: str | list[ResponsesPart]


class ResponsesCustomToolCall(_LenientModel):
    type: Literal["custom_tool_call"]
    call_id: str
    name: str
    input: str


class ResponsesCustomToolCallOutput(_LenientModel):
    type: Literal["custom_tool_call_output"]
    call_id: str
    output: str | list[ResponsesPart]


class ResponsesSummaryText(_LenientModel):
    text: str


class ResponsesReasoning(_LenientModel):
    type: Literal["reasoning"]
    summary: list[ResponsesSummaryText] = Field(default_factory=list)


class ResponsesWebSearchCall(_LenientModel):
    type: Literal["web_search_call"]
    id: str | None = None
    action: JsonValue = None


class ResponsesCompaction(_LenientModel):
    type: Literal["compaction"]
    encrypted_content: str | None = None


class ResponsesUnknownItem(_LenientModel):
    type: str


ResponsesItem = Annotated[
    ResponsesMessage
    | ResponsesFunctionCall
    | ResponsesFunctionCallOutput
    | ResponsesCustomToolCall
    | ResponsesCustomToolCallOutput
    | ResponsesReasoning
    | ResponsesWebSearchCall
    | ResponsesCompaction
    | ResponsesUnknownItem,
    Field(union_mode="left_to_right"),
]

# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


class ResponsesTool(_LenientModel):
    type: str
    name: str | None = None
    description: str | None = None
    parameters: JsonValue = None


class ResponsesRequest(_LenientModel):
    model: str | None = None
    input: str | list[ResponsesItem]
    instructions: str | None = None
    tools: list[ResponsesTool] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


class ResponsesInputTokensDetails(_LenientModel):
    cached_tokens: int = 0
    cache_write_tokens: int = 0


class ResponsesOutputTokensDetails(_LenientModel):
    reasoning_tokens: int = 0


class ResponsesUsage(_LenientModel):
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_details: ResponsesInputTokensDetails | None = None
    output_tokens_details: ResponsesOutputTokensDetails | None = None


class ResponsesIncompleteDetails(_LenientModel):
    reason: str | None = None


class Response(_LenientModel):
    object: Literal["response"]
    id: str
    status: str | None = None
    output: list[ResponsesItem] = Field(default_factory=list)
    usage: ResponsesUsage | None = None
    incomplete_details: ResponsesIncompleteDetails | None = None
    model: str | None = None


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


class ResponseStreamEvent(_LenientModel):
    # Any ``type``, so an SSE ``error`` event doesn't fail the record.
    type: str
    response: Response | None = None


_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})


def final_response(events: list[ResponseStreamEvent]) -> Response | None:
    """The ``response`` carried by the last terminal stream event."""
    for event in reversed(events):
        if event.type in _TERMINAL_EVENTS:
            return event.response
    return None
