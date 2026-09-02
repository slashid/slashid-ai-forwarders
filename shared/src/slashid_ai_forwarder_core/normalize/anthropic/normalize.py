"""Anthropic Messages API → NormalizedInvocation translates + helpers.

Two entry points:
- ``message_to_normalized_invocation(request, response)`` — non-streaming
- ``stream_to_normalized_invocation(request, response)`` — streaming state-machine

Plus one helper used by the Bedrock forwarder's envelope handling:
- ``extract_stream_usage(events)`` — collapses per-event usage counts
  into a single ``AnthropicUsage`` for the MIL top-level token backfill.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from ..normalized.tools import build_tools_declared
from ..normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)
from .schema import (
    AnthropicContentBlockDeltaEvent,
    AnthropicContentBlockStart,
    AnthropicContentBlockStop,
    AnthropicInputJsonDelta,
    AnthropicMessage,
    AnthropicMessageDelta,
    AnthropicMessageStart,
    AnthropicRequestBody,
    AnthropicRequestContentBlock,
    AnthropicStreamEvent,
    AnthropicSystemBlock,
    AnthropicTextBlock,
    AnthropicTextDelta,
    AnthropicThinkingBlock,
    AnthropicThinkingDelta,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
    AnthropicUsage,
)
from .stop_reasons import map as map_anthropic_stop_reason

log = logging.getLogger(__name__)


def extract_stream_usage(events: list[AnthropicStreamEvent]) -> AnthropicUsage:
    """Collapse ``message_start.message.usage`` + ``message_delta.usage`` into one usage.

    May return a partially-populated ``AnthropicUsage`` (any subset of fields
    can be None). Typical shape: ``message_start`` carries initial
    ``input_tokens`` + ``cache_read_input_tokens``; ``message_delta`` carries
    final ``output_tokens``. Later values overwrite earlier ones on key
    collision, matching ``_legacy.py::extract_anthropic_stream_usage``.
    """
    out = AnthropicUsage()
    for event in events:
        match event:
            case AnthropicMessageStart() if (
                event.message is not None and event.message.usage is not None
            ):
                for field, value in event.message.usage.model_dump(exclude_none=True).items():
                    setattr(out, field, value)
            case AnthropicMessageDelta() if event.usage is not None:
                for field, value in event.usage.model_dump(exclude_none=True).items():
                    setattr(out, field, value)
    return out


def message_to_normalized_invocation(
    request: AnthropicRequestBody,
    response: AnthropicMessage,
) -> NormalizedInvocation:
    """Non-streaming Anthropic invocation → canonical NormalizedInvocation.

    Direct walks on both sides (no composition through Converse) —
    Anthropic-side fields (cache_control markers, is_error on tool_result
    blocks, thinking signatures) preserved without a lossy hop.
    """
    return NormalizedInvocation(
        input=_request_to_input(request),
        output=_message_to_output(response),
    )


def _request_to_input(request: AnthropicRequestBody) -> NormalizedInvocationInput:
    """Walk an Anthropic request body → NormalizedInvocationInput.

    Anthropic's ``system`` can be a bare string or a list of
    ``AnthropicSystemBlock`` entries; both forms fold into a single
    index-0 ``role="system"`` NormalizedMessage per canonical convention.
    Tool declarations from ``request.tools`` become canonical ``AITool`` /
    ``AIToolServer`` lists via ``build_tools_declared`` — Claude Code's
    ``mcp__server__tool`` naming pattern is parsed the same way as
    Converse's ``toolConfig`` entries.
    """
    messages: list[NormalizedMessage] = []
    system_text = _flatten_system(request.system)
    if system_text is not None:
        messages.append(
            NormalizedMessage(
                role="system",
                content=[NormalizedContent(kind="text", text=system_text)],
            )
        )
    for msg in request.messages:
        messages.append(
            NormalizedMessage(
                role=msg.role,
                content=_translate_request_content(msg.content),
            )
        )
    tools_declared, tool_servers = build_tools_declared(
        (t.name, t.description, t.input_schema) for t in (request.tools or [])
    )
    return NormalizedInvocationInput(
        messages=messages or None,
        tools_declared=tools_declared or None,
        tool_servers=tool_servers or None,
    )


def _flatten_system(
    system: str | list[AnthropicSystemBlock] | None,
) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    # List form — concatenate text blocks.
    return "".join(block.text or "" for block in system)


def _translate_request_content(
    blocks: list[AnthropicRequestContentBlock],
) -> list[NormalizedContent]:
    out: list[NormalizedContent] = []
    for block in blocks:
        match block:
            case AnthropicTextBlock():
                out.append(NormalizedContent(kind="text", text=block.text))
            case AnthropicToolUseBlock():
                out.append(
                    NormalizedContent(
                        kind="tool_use",
                        tool_use_id=block.id,
                        tool_name=block.name,
                        tool_input=block.input if block.input else {},
                        tool_executor="client",
                    )
                )
            case AnthropicThinkingBlock():
                out.append(NormalizedContent(kind="reasoning", text=block.thinking))
            case AnthropicToolResultBlock():
                out.append(
                    NormalizedContent(
                        kind="tool_result",
                        tool_use_id=block.tool_use_id,
                        tool_output=block.content,
                        tool_is_error=block.is_error,
                        tool_executor="client",
                    )
                )
            # AnthropicUnknownBlock: skipped silently.
    return out


def _message_to_output(msg: AnthropicMessage) -> NormalizedInvocationOutput:
    """Walk an Anthropic non-streaming response → NormalizedInvocationOutput.

    Byte-parity note: matches the null-input-becomes-empty-dict invariant
    from Phase 1.1 (tool_use blocks with input=None or falsy → {}). This
    is preserved because content hashing is sensitive to the distinction
    between "empty dict" and "null".
    """
    content: list[NormalizedContent] = []
    for block in msg.content:
        match block:
            case AnthropicTextBlock():
                content.append(NormalizedContent(kind="text", text=block.text))
            case AnthropicToolUseBlock():
                content.append(
                    NormalizedContent(
                        kind="tool_use",
                        tool_use_id=block.id,
                        tool_name=block.name,
                        tool_input=block.input if block.input else {},
                        tool_executor="client",
                    )
                )
            case AnthropicThinkingBlock():
                content.append(NormalizedContent(kind="reasoning", text=block.thinking))
            # AnthropicUnknownBlock: skipped silently.
    return NormalizedInvocationOutput(
        message=NormalizedMessage(role="assistant", content=content),
        stop_reason=map_anthropic_stop_reason(msg.stop_reason),
    )


# --------------------------------------------------------------------------
# Supported test helper — bridges a raw Anthropic-shape MIL record dict into
# NormalizedInvocation with best-effort validation on each side. Mirrors
# ``converse.normalize.converse_dict_to_normalized`` for symmetry; production
# Bedrock forwarder uses ``mil_normalize.normalize_record`` directly.
# --------------------------------------------------------------------------


def anthropic_dict_to_normalized(record: dict) -> NormalizedInvocation:  # type: ignore[type-arg]
    """Convenience adapter: MIL/Anthropic-dict record → NormalizedInvocation.

    Best-effort on each side — validation failure falls back to the empty
    default rather than raising. Useful for tests / audit-envelope replay
    where the caller holds a raw Anthropic-shape input body (with
    ``messages[].content[]`` carrying ``{"type": "tool_use", ...}`` etc.).

    Non-streaming (``AnthropicMessage``) shape only — streaming records
    should route through ``mil_normalize.normalize_record`` in the Bedrock
    forwarder.
    """
    from pydantic import TypeAdapter, ValidationError

    _request_adapter = TypeAdapter(AnthropicRequestBody)
    _response_adapter = TypeAdapter(AnthropicMessage)

    in_body = (record.get("input") or {}).get("inputBodyJson")
    out_body = (record.get("output") or {}).get("outputBodyJson")

    input_side: NormalizedInvocationInput
    try:
        parsed_in = _request_adapter.validate_python(in_body)
    except ValidationError:
        input_side = NormalizedInvocationInput()
    else:
        input_side = _request_to_input(parsed_in)

    output_side: NormalizedInvocationOutput
    try:
        parsed_out = _response_adapter.validate_python(out_body)
    except ValidationError:
        output_side = NormalizedInvocationOutput()
    else:
        output_side = _message_to_output(parsed_out)

    return NormalizedInvocation(input=input_side, output=output_side)


def stream_to_normalized_invocation(
    request: AnthropicRequestBody,
    response: list[AnthropicStreamEvent],
) -> NormalizedInvocation:
    """Streaming Anthropic invocation → canonical.

    Same shape as ``message_to_normalized_invocation``; the output side
    runs a state-machine over the SSE event stream, producing
    NormalizedContent blocks directly with no Converse hop.
    """
    return NormalizedInvocation(
        input=_request_to_input(request),
        output=_stream_to_output(response),
    )


def _stream_to_output(
    events: list[AnthropicStreamEvent],
) -> NormalizedInvocationOutput:
    """Walk Anthropic SSE events → NormalizedInvocationOutput.

    Byte-parity notes:
    - malformed ``input_json_delta`` reassembly (buffer doesn't parse
      as JSON) logs a WARNING with byte count and produces ``{}`` as
      the tool_use input (never the raw bytes — could contain PII);
    - null / empty tool_use input becomes ``{}``, not ``None``.
    """
    content: list[NormalizedContent] = []
    slots: dict[int, dict[str, Any]] = {}
    stop_reason: str | None = None

    for event in events:
        match event:
            case AnthropicContentBlockStart():
                cb = event.content_block
                match cb:
                    case AnthropicTextBlock():
                        slots[event.index] = {"_type": "text", "text": cb.text}
                    case AnthropicToolUseBlock():
                        slots[event.index] = {
                            "_type": "tool_use",
                            "tool_use_id": cb.id,
                            "name": cb.name,
                            "input_buf": "",
                        }
                    case AnthropicThinkingBlock():
                        slots[event.index] = {"_type": "reasoning", "text": ""}
                    # AnthropicUnknownBlock: no slot allocated.

            case AnthropicContentBlockDeltaEvent():
                slot = slots.get(event.index)
                if slot is None:
                    continue
                match event.delta:
                    case AnthropicTextDelta():
                        slot["text"] = slot.get("text", "") + event.delta.text
                    case AnthropicInputJsonDelta():
                        slot["input_buf"] = slot.get("input_buf", "") + event.delta.partial_json
                    case AnthropicThinkingDelta():
                        slot["text"] = slot.get("text", "") + event.delta.thinking
                    # AnthropicUnknownDelta: silently ignored.

            case AnthropicContentBlockStop():
                slot = slots.pop(event.index, None)
                if slot is None:
                    continue
                block_type = slot["_type"]
                if block_type == "text":
                    content.append(
                        NormalizedContent(
                            kind="text",
                            text=slot.get("text", ""),
                        )
                    )
                elif block_type == "reasoning":
                    content.append(
                        NormalizedContent(
                            kind="reasoning",
                            text=slot.get("text", ""),
                        )
                    )
                elif block_type == "tool_use":
                    parsed_input: dict[str, Any] = {}
                    buf = slot.get("input_buf") or ""
                    if buf:
                        try:
                            parsed_input = cast("dict[str, Any]", json.loads(buf))
                        except json.JSONDecodeError:
                            log.warning("tool_use input_json malformed (%d bytes)", len(buf))
                    content.append(
                        NormalizedContent(
                            kind="tool_use",
                            tool_use_id=slot["tool_use_id"],
                            tool_name=slot["name"],
                            tool_input=parsed_input,
                            tool_executor="client",
                        )
                    )

            case AnthropicMessageDelta() if event.delta:
                raw_reason = event.delta.get("stop_reason")
                if isinstance(raw_reason, str) and raw_reason:
                    stop_reason = raw_reason

    return NormalizedInvocationOutput(
        message=NormalizedMessage(role="assistant", content=content),
        stop_reason=map_anthropic_stop_reason(stop_reason),
    )
