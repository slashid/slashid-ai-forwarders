"""Converse stream events → ``ConverseResponse``.

``ConverseStream`` and the Converse-shaped ``InvokeModelWithResponseStream``
(Nova) emit one event per list entry, each keyed by its type. MIL reassembles
a ``ConverseStream`` call itself, but logs the raw events for ``InvokeModel``.
Text and reasoning blocks have no start event, so blocks are keyed by
``contentBlockIndex``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import AfterValidator, Field, JsonValue, model_validator

from .._base import _LenientModel
from .schema import (
    ConverseAssistantMessage,
    ConverseContentBlock,
    ConverseOutput,
    ConverseReasoningBlock,
    ConverseReasoningContent,
    ConverseReasoningText,
    ConverseResponse,
    ConverseTextBlock,
    ConverseToolUse,
    ConverseToolUseBlock,
)


class ConverseStartToolUse(_LenientModel):
    toolUseId: str
    name: str


class ConverseBlockStart(_LenientModel):
    toolUse: ConverseStartToolUse | None = None


class ConverseContentBlockStart(_LenientModel):
    contentBlockIndex: int = 0
    start: ConverseBlockStart = Field(default_factory=ConverseBlockStart)


class ConverseDeltaToolUse(_LenientModel):
    input: str = ""


class ConverseDeltaReasoning(_LenientModel):
    text: str | None = None
    signature: str | None = None
    redactedContent: str | None = None


class ConverseBlockDelta(_LenientModel):
    text: str | None = None
    toolUse: ConverseDeltaToolUse | None = None
    reasoningContent: ConverseDeltaReasoning | None = None


class ConverseContentBlockDelta(_LenientModel):
    contentBlockIndex: int = 0
    delta: ConverseBlockDelta = Field(default_factory=ConverseBlockDelta)


class ConverseMessageStop(_LenientModel):
    stopReason: str | None = None


class ConverseMetadata(_LenientModel):
    usage: JsonValue = None


class ConverseStreamEvent(_LenientModel):
    messageStart: dict[str, JsonValue] | None = None
    contentBlockStart: ConverseContentBlockStart | None = None
    contentBlockDelta: ConverseContentBlockDelta | None = None
    contentBlockStop: dict[str, JsonValue] | None = None
    messageStop: ConverseMessageStop | None = None
    metadata: ConverseMetadata | None = None

    @model_validator(mode="after")
    def _is_a_converse_event(self) -> ConverseStreamEvent:
        if not any(getattr(self, key) is not None for key in type(self).model_fields):
            raise ValueError("not a converse stream event")
        return self


def _require_converse_stream(events: list[ConverseStreamEvent]) -> list[ConverseStreamEvent]:
    if not any(e.messageStart is not None or e.messageStop is not None for e in events):
        raise ValueError("no messageStart or messageStop event")
    return events


# A stream with a message boundary, so any list of one-key dicts isn't taken for one.
ConverseStream = Annotated[list[ConverseStreamEvent], AfterValidator(_require_converse_stream)]


@dataclass
class _Block:
    text: str = ""
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: str = ""
    reasoning: str | None = None
    signature: str | None = None
    redacted: str | None = None
    kinds: set[str] = field(default_factory=set)


def accumulate_stream(events: list[ConverseStreamEvent]) -> ConverseResponse | None:
    """Fold stream events into the response a non-streaming call would return."""
    if not events:
        return None
    blocks: dict[int, _Block] = {}
    stop_reason: str | None = None
    usage: JsonValue = None
    for event in events:
        if (start := event.contentBlockStart) is not None and start.start.toolUse is not None:
            block = blocks.setdefault(start.contentBlockIndex, _Block())
            block.kinds.add("tool")
            block.tool_use_id, block.tool_name = (
                start.start.toolUse.toolUseId,
                start.start.toolUse.name,
            )
        if (delta := event.contentBlockDelta) is not None:
            _apply_delta(blocks.setdefault(delta.contentBlockIndex, _Block()), delta.delta)
        if event.messageStop is not None:
            stop_reason = event.messageStop.stopReason or stop_reason
        if event.metadata is not None and event.metadata.usage is not None:
            usage = event.metadata.usage
    content = [_content_block(block) for block in _coalesce([b for _, b in sorted(blocks.items())])]
    return ConverseResponse(
        output=ConverseOutput(
            message=ConverseAssistantMessage(
                role="assistant", content=[c for c in content if c is not None]
            )
        ),
        stopReason=stop_reason,
        usage=usage,
    )


def _coalesce(blocks: list[_Block]) -> list[_Block]:
    """Nova's native stream gives every delta its own index; adjacent text (or
    reasoning) blocks are one block in the response. A tool use, or a
    reasoning signature, ends a block."""
    merged: list[_Block] = []
    for block in blocks:
        if merged and _continues(merged[-1], block):
            merged[-1] = _join(merged[-1], block)
        else:
            merged.append(block)
    return merged


def _continues(previous: _Block, block: _Block) -> bool:
    if previous.kinds == {"text"} and block.kinds == {"text"}:
        return True
    return (
        previous.kinds == {"reasoning"}
        and block.kinds == {"reasoning"}
        and previous.signature is None
        and previous.redacted is None
        and block.redacted is None
    )


def _join(previous: _Block, block: _Block) -> _Block:
    reasoning = previous.reasoning
    if block.reasoning is not None:
        reasoning = (reasoning or "") + block.reasoning
    return _Block(
        text=previous.text + block.text,
        reasoning=reasoning,
        signature=block.signature,
        kinds=set(previous.kinds),
    )


def _apply_delta(block: _Block, delta: ConverseBlockDelta) -> None:
    if delta.text is not None:
        block.kinds.add("text")
        block.text += delta.text
    if delta.toolUse is not None:
        block.kinds.add("tool")
        block.tool_input += delta.toolUse.input
    if (reasoning := delta.reasoningContent) is not None:
        block.kinds.add("reasoning")
        if reasoning.text is not None:
            block.reasoning = (block.reasoning or "") + reasoning.text
        block.signature = reasoning.signature or block.signature
        block.redacted = reasoning.redactedContent or block.redacted


def _content_block(block: _Block) -> ConverseContentBlock | None:
    if "tool" in block.kinds and block.tool_use_id is not None and block.tool_name is not None:
        return ConverseToolUseBlock(
            toolUse=ConverseToolUse(
                toolUseId=block.tool_use_id,
                name=block.tool_name,
                input=_json_or_raw(block.tool_input),
            )
        )
    if "reasoning" in block.kinds:
        return ConverseReasoningBlock(
            reasoningContent=ConverseReasoningContent(
                reasoningText=ConverseReasoningText(
                    text=block.reasoning or "", signature=block.signature
                )
                if block.reasoning is not None or block.signature is not None
                else None,
                redactedContent=block.redacted,
            )
        )
    if "text" in block.kinds:
        return ConverseTextBlock(text=block.text)
    return None


def _json_or_raw(fragments: str) -> JsonValue:
    if not fragments.strip():
        return {}
    try:
        return json.loads(fragments)
    except json.JSONDecodeError:
        return fragments
