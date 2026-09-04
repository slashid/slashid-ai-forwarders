"""Pydantic schemas for the Anthropic Messages API wire shapes we consume."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue
from pydantic.json_schema import JsonSchemaValue

from .._base import _LenientModel

# --------------------------------------------------------------------------
# Usage (also appears inside stream events)
# --------------------------------------------------------------------------


class AnthropicUsage(_LenientModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


# --------------------------------------------------------------------------
# Content blocks (shared between response messages and stream
# content_block_start descriptors — defaults make both usages valid).
# --------------------------------------------------------------------------


class AnthropicTextBlock(_LenientModel):
    type: Literal["text"]
    text: str = ""


class AnthropicToolUseBlock(_LenientModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: JsonValue = None  # absent on stream start, filled by input_json_delta


class AnthropicThinkingBlock(_LenientModel):
    type: Literal["thinking"]
    thinking: str = ""  # empty on stream start, appended by thinking_delta
    signature: str | None = None


class AnthropicUnknownBlock(_LenientModel):
    """Catch-all for content-block types we don't model yet
    (server_tool_use, web_search_tool_result, redacted_thinking, ...).
    Passed through as a no-op in transforms; may become warn-once in a
    follow-up. Phase 1.1 skips silently."""

    type: str


AnthropicContentBlock = (
    AnthropicTextBlock | AnthropicToolUseBlock | AnthropicThinkingBlock | AnthropicUnknownBlock
)
# Bare union (no Field(discriminator=...)) because AnthropicUnknownBlock's
# ``type: str`` isn't a Literal — pydantic requires Literal discriminators.
# Smart-union picks the most specific match: Literal["text"] beats bare str
# when values match, so a text block validates as AnthropicTextBlock rather
# than falling through to AnthropicUnknownBlock.


# --------------------------------------------------------------------------
# Non-streaming response message
# --------------------------------------------------------------------------


class AnthropicMessage(_LenientModel):
    type: Literal["message"]
    role: Literal["assistant"]
    content: list[AnthropicContentBlock] = Field(default_factory=list)
    stop_reason: str | None = None
    usage: AnthropicUsage | None = None


# --------------------------------------------------------------------------
# Request-side tool declarations (only piece of the request we transform)
# --------------------------------------------------------------------------


class AnthropicToolDeclaration(_LenientModel):
    name: str
    description: str | None = None
    input_schema: JsonSchemaValue | None = None


# --------------------------------------------------------------------------
# Streaming events
#
# Content-block descriptors inside ``content_block_start`` reuse the
# AnthropicContentBlock union from above. Defaults make this work:
#   - AnthropicTextBlock.text has default "" (stream sends empty on start;
#     text_delta appends).
#   - AnthropicToolUseBlock.input has default None (input arrives via
#     input_json_delta and is reassembled at content_block_stop).
#   - AnthropicThinkingBlock.thinking / .signature default to empty/None
#     (thinking_delta appends; signature_delta finalises).
# --------------------------------------------------------------------------


class AnthropicTextDelta(_LenientModel):
    type: Literal["text_delta"]
    text: str = ""


class AnthropicInputJsonDelta(_LenientModel):
    type: Literal["input_json_delta"]
    partial_json: str = ""


class AnthropicThinkingDelta(_LenientModel):
    type: Literal["thinking_delta"]
    thinking: str = ""


class AnthropicUnknownDelta(_LenientModel):
    type: str


AnthropicDelta = (
    AnthropicTextDelta | AnthropicInputJsonDelta | AnthropicThinkingDelta | AnthropicUnknownDelta
)  # smart-union; see AnthropicContentBlock for the same rationale.


class AnthropicMessageStartPayload(_LenientModel):
    """The `message` payload inside a ``message_start`` event.

    Deliberately lax — real Anthropic wire carries the full AnthropicMessage
    envelope (type/role/content/model/…), but some MIL captures strip it
    down to just ``usage``. We only rely on ``usage`` here; everything else
    is dropped via ``extra="ignore"``.
    """

    usage: AnthropicUsage | None = None


class AnthropicMessageStart(_LenientModel):
    type: Literal["message_start"]
    message: AnthropicMessageStartPayload | None = None


class AnthropicMessageDelta(_LenientModel):
    type: Literal["message_delta"]
    delta: dict[str, JsonValue] | None = None
    usage: AnthropicUsage | None = None


class AnthropicMessageStop(_LenientModel):
    type: Literal["message_stop"]


class AnthropicContentBlockStart(_LenientModel):
    type: Literal["content_block_start"]
    index: int
    content_block: AnthropicContentBlock  # reuses the response-side union


class AnthropicContentBlockDeltaEvent(_LenientModel):
    type: Literal["content_block_delta"]
    index: int
    delta: AnthropicDelta


class AnthropicContentBlockStop(_LenientModel):
    type: Literal["content_block_stop"]
    index: int


class AnthropicPing(_LenientModel):
    type: Literal["ping"]


# NOTE: intentionally no catch-all AnthropicUnknownEvent variant. If a stream
# contains an event with an unknown top-level `type`, `list[AnthropicStreamEvent]`
# validation fails and the dispatcher falls through to `parsed_as="unknown"`.
# This is load-bearing for stream detection: non-Anthropic Bedrock streams
# (Nova/Titan/Cohere use their own event vocabulary) MUST fail to validate
# here so mil_normalize doesn't wrongly claim ownership of them. Anthropic
# adds new event types rarely — when they do, we'll see the WARNING and add
# the class here. Content-block-level unknown-tolerance still lives on
# AnthropicUnknownBlock / AnthropicUnknownDelta (inner shapes where new
# variants are more common).
AnthropicStreamEvent = (
    AnthropicMessageStart
    | AnthropicMessageDelta
    | AnthropicMessageStop
    | AnthropicContentBlockStart
    | AnthropicContentBlockDeltaEvent
    | AnthropicContentBlockStop
    | AnthropicPing
)


# --------------------------------------------------------------------------
# Request-side schemas (Phase 2)
# --------------------------------------------------------------------------


class AnthropicSystemBlock(_LenientModel):
    """One entry of the list form of ``AnthropicRequestBody.system``.

    Anthropic's system prompt can be a bare string OR a list of
    ``{type: "text", text: ...}`` blocks (with cache_control markers etc.
    ignored by _LenientModel).
    """

    type: Literal["text"] = "text"
    text: str = ""


class AnthropicToolResultBlock(_LenientModel):
    """User-turn content block carrying a tool execution result back to the model.

    Not part of the response-side ``AnthropicContentBlock`` union — only
    valid inside request messages with ``role="user"``.
    ``is_error`` defaults to False (absence = success, per Anthropic's docs).
    """

    type: Literal["tool_result"]
    tool_use_id: str
    content: JsonValue = None  # str | list[block] | dict — kept as JsonValue
    is_error: bool = False


AnthropicRequestContentBlock = (
    AnthropicTextBlock
    | AnthropicToolUseBlock
    | AnthropicThinkingBlock
    | AnthropicToolResultBlock
    | AnthropicUnknownBlock
)
# Superset of the response-side content-block union: adds
# AnthropicToolResultBlock, which only ever appears in user-turn content.


class AnthropicRequestMessage(_LenientModel):
    """One message in ``AnthropicRequestBody.messages``.

    ``role`` is broader than the response side (which is only "assistant"):
    request-side messages are the conversation history, so both user and
    assistant turns appear.
    """

    role: Literal["user", "assistant"]
    content: list[AnthropicRequestContentBlock]


class AnthropicRequestBody(_LenientModel):
    """The request body sent to Anthropic's Messages API.

    Content-relevant fields only — non-content settings (max_tokens,
    temperature, top_p, stream, metadata, ...) are dropped via
    _LenientModel's extra="ignore". Those are how, not what, and
    shouldn't affect content hashing.
    """

    system: str | list[AnthropicSystemBlock] | None = None
    messages: list[AnthropicRequestMessage]
    tools: list[AnthropicToolDeclaration] | None = None
    tool_choice: JsonValue = None  # loose — not walked by normalizers today
