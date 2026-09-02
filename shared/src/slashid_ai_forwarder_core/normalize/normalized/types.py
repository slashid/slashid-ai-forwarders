"""Canonical, vendor-neutral pydantic models for one AI invocation.

Produced by each vendor's ``*_to_normalized_invocation(request, response)``
translate; consumed by ``events.build_event`` (content hashing, tool-use
extraction, log emission). The sub-model split (input vs output vs
tokens) mirrors the wire event's shape and lets ``build_event`` hash each
half independently via ``normalized.input.model_dump(...)`` /
``normalized.output.model_dump(...)``.

``_LenientModel`` is used throughout so unknown fields are dropped
silently — safe against future extensions and per-vendor quirks that
squeak through the vendor schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, NonNegativeInt
from pydantic_extra_types.mime_types import MimeType

from .._base import _LenientModel
from ...events import AIInvocationTokens, AIStopReason, AITool, AIToolServer

# ``.._base`` reaches ``normalize/_base`` (two levels up: normalized/ → normalize/).
# ``...events`` reaches ``slashid_ai_forwarder_core.events`` (three levels up).
# Canonical types deliberately share the vendor packages' _LenientModel
# rather than duplicating it here — keeps the base-class semantics uniform.


class NormalizedContent(_LenientModel):
    """One content block inside a message — the smallest unit of what the
    conversation is *about*."""

    kind: Literal[
        "text", "image", "audio", "document", "tool_use", "tool_result", "reasoning"
    ]
    text: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: JsonValue = None
    tool_output: JsonValue = None
    tool_is_error: bool = False
    tool_executor: Literal["client", "server"] | None = None
    media_type: MimeType | None = None
    byte_length: NonNegativeInt | None = None


class NormalizedMessage(_LenientModel):
    """One turn in the conversation.

    ``system`` messages carry system-prompt content and — by convention —
    appear at index 0 of ``NormalizedInvocationInput.messages`` when
    present, matching the OpenAI Chat Completions ordering. ``user`` /
    ``assistant`` messages carry the back-and-forth. ``tool`` messages
    carry tool-execution results (rare — most vendors inline these into
    user-turn ``tool_result`` blocks).
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: list[NormalizedContent]


class NormalizedInvocationInput(_LenientModel):
    """Canonical input-side shape — the request body's content-relevant fields.

    Non-content fields (temperature, max_tokens, top_p, stream, etc.) are
    deliberately excluded: they're vendor-specific settings, not content.
    Excluding them means "same conversation with different sampling
    parameters" hashes to the same input — a feature, not a bug.
    """

    messages: list[NormalizedMessage] | None = None
    tools_declared: list[AITool] | None = None
    tool_servers: list[AIToolServer] | None = None


class NormalizedInvocationOutput(_LenientModel):
    """Canonical output-side shape — the assistant response produced by this call."""

    message: NormalizedMessage | None = None
    stop_reason: AIStopReason = "unknown"


class NormalizedInvocation(_LenientModel):
    """Full canonical shape for one AI invocation.

    The sub-model split (``input`` / ``output`` / ``tokens``) mirrors the
    wire event's shape (``AIInvocationObservedV1.input`` /
    ``AIInvocationObservedV1.output`` / ``AIInvocationObservedV1.tokens``)
    and lets ``build_event`` hash each half independently. An empty
    ``NormalizedInvocation()`` is the ``parsed_as="unknown"`` fallthrough
    — every field defaults so no vendor is needed to construct one.
    """

    tokens: AIInvocationTokens = Field(default_factory=AIInvocationTokens)
    input: NormalizedInvocationInput = Field(default_factory=NormalizedInvocationInput)
    output: NormalizedInvocationOutput = Field(default_factory=NormalizedInvocationOutput)
