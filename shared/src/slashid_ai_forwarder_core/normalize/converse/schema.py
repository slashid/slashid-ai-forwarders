"""Pydantic schemas for the AWS Bedrock Converse API wire shapes.

Content blocks are key-tagged (one of ``text`` / ``toolUse`` /
``reasoningContent`` / ``image`` / ``document`` / ``video`` / ``cachePoint``
/ ``guardContent`` / ``toolResult`` as the single top-level key). Modeled
via ``_StrictModel`` variants so pydantic's smart-union picks the variant
whose key IS present; ``ConverseUnknownBlock`` is the catch-all fallback.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, JsonValue
from pydantic.json_schema import JsonSchemaValue

from .._base import _LenientModel, _StrictModel

# --------------------------------------------------------------------------
# Content-block payload types
# --------------------------------------------------------------------------


class ConverseToolUse(_LenientModel):
    toolUseId: str
    name: str
    input: JsonValue = None


class ConverseReasoningText(_LenientModel):
    text: str = ""
    signature: str | None = None


class ConverseReasoningContent(_LenientModel):
    reasoningText: ConverseReasoningText | None = None
    redactedContent: str | None = None  # base64


# --------------------------------------------------------------------------
# Content blocks — key-tagged discriminated union
# ORDER MATTERS: pydantic smart-union picks the first strict variant that
# validates; ConverseUnknownBlock is last and always accepts.
# --------------------------------------------------------------------------


class ConverseTextBlock(_StrictModel):
    text: str


class ConverseToolUseBlock(_StrictModel):
    toolUse: ConverseToolUse


class ConverseReasoningBlock(_StrictModel):
    reasoningContent: ConverseReasoningContent


class ConverseUnknownBlock(_LenientModel):
    """Catch-all for keys we don't model (image, document, video, cachePoint,
    guardContent, toolResult, ...). Passed through as-is on dump; the
    original key is preserved via model_dump."""


ConverseContentBlock = (
    ConverseTextBlock | ConverseToolUseBlock | ConverseReasoningBlock | ConverseUnknownBlock
)


# --------------------------------------------------------------------------
# Response envelope
# --------------------------------------------------------------------------


class ConverseAssistantMessage(_LenientModel):
    role: Literal["assistant"]
    content: list[ConverseContentBlock] = Field(default_factory=list)


class ConverseOutput(_LenientModel):
    message: ConverseAssistantMessage


class ConverseResponse(_LenientModel):
    """The output.outputBodyJson shape for a Converse response — matches
    Bedrock's ConverseResponse and what MIL emits for native-Converse models.
    """

    output: ConverseOutput
    stopReason: str | None = None
    usage: JsonValue = None  # loose — MIL top-level tokens are authoritative


# --------------------------------------------------------------------------
# Tool config (translate output for the Anthropic-tools rewrite path)
#
# `json` is a Python builtin, so we use `json_` in Python and alias it to
# `json` on the wire. ConfigDict here overrides the inherited _LenientModel
# config to add populate_by_name.
# --------------------------------------------------------------------------


class ConverseToolInputSchema(_LenientModel):
    json_: JsonSchemaValue = Field(alias="json")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ConverseToolSpec(_LenientModel):
    name: str
    description: str | None = None
    inputSchema: ConverseToolInputSchema


class ConverseTool(_LenientModel):
    toolSpec: ConverseToolSpec


class ConverseToolConfig(_LenientModel):
    tools: list[ConverseTool] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Request-side schemas (Phase 2)
# --------------------------------------------------------------------------


class ConverseSystemContentBlock(_LenientModel):
    """One entry of ``ConverseRequestBody.system``.

    Bedrock system is a ``list[SystemContentBlock]`` where each block
    carries ``{text: "..."}`` (canonical) — variants like ``{guardContent:
    ...}`` are dropped via _LenientModel.
    """

    text: str = ""


class ConverseToolResultContent(_LenientModel):
    """Inner ``toolResult`` payload — the ``{toolUseId, content, status}`` shape."""

    toolUseId: str
    content: JsonValue = None  # list[block] | str — kept loose
    status: Literal["success", "error"] | None = None


class ConverseToolResultBlock(_StrictModel):
    """User-turn content block carrying a tool result back to the model.

    Key-tagged with ``toolResult`` (matches the pattern of ``ConverseTextBlock`` /
    ``ConverseToolUseBlock`` / ``ConverseReasoningBlock``). Strict so
    smart-union picks the variant whose key IS present.
    """

    toolResult: ConverseToolResultContent


ConverseRequestContentBlock = (
    ConverseTextBlock
    | ConverseToolUseBlock
    | ConverseReasoningBlock
    | ConverseToolResultBlock
    | ConverseUnknownBlock
)
# Superset of response-side ConverseContentBlock, adding
# ConverseToolResultBlock for user-turn content.


class ConverseRequestMessage(_LenientModel):
    """One message in ``ConverseRequestBody.messages``.

    Role widened from response-side ``Literal["assistant"]`` to
    ``Literal["user", "assistant"]`` (request-side is the conversation
    history).
    """

    role: Literal["user", "assistant"]
    content: list[ConverseRequestContentBlock]


class ConverseRequestBody(_LenientModel):
    """The request body sent to Bedrock Converse API.

    Content-relevant fields only — inferenceConfig,
    additionalModelRequestFields, guardrailConfig, etc. are dropped via
    _LenientModel.
    """

    system: list[ConverseSystemContentBlock] | None = None
    messages: list[ConverseRequestMessage]
    toolConfig: ConverseToolConfig | None = None
