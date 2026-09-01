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
