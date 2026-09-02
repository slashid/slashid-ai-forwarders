"""Anthropic -> Converse translates (pure functions, no envelope knowledge).

Byte-parity with the pre-refactor helpers in ``_legacy.py``
(``_reconstruct_message_from_dict``, ``_reconstruct_message_from_stream``,
``anthropic_tools_to_converse_tool_config``, ``extract_anthropic_stream_usage``).
The dispatcher rewrite in Chunk 4 will retarget ``mil_normalize.py`` to
these typed entry points; ``_legacy.py`` gets deleted in Chunk 6.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from ..converse.schema import (
    ConverseAssistantMessage,
    ConverseContentBlock,
    ConverseOutput,
    ConverseResponse,
    ConverseTextBlock,
    ConverseTool,
    ConverseToolConfig,
    ConverseToolInputSchema,
    ConverseToolSpec,
    ConverseToolUse,
    ConverseToolUseBlock,
)
from .schema import (
    AnthropicContentBlockDeltaEvent,
    AnthropicContentBlockStart,
    AnthropicContentBlockStop,
    AnthropicInputJsonDelta,
    AnthropicMessage,
    AnthropicMessageDelta,
    AnthropicMessageStart,
    AnthropicStreamEvent,
    AnthropicTextBlock,
    AnthropicTextDelta,
    AnthropicThinkingBlock,
    AnthropicThinkingDelta,
    AnthropicToolDeclaration,
    AnthropicToolUseBlock,
    AnthropicUsage,
)

log = logging.getLogger(__name__)


def message_to_converse(msg: AnthropicMessage) -> ConverseResponse:
    """Rewrite an Anthropic non-streaming Messages response into Converse shape.

    Empty content list is valid — returns a ``ConverseResponse`` whose
    ``output.message.content`` is also empty. Unknown-type blocks
    (``AnthropicUnknownBlock``) are skipped silently in 1.1.

    Byte-parity with ``_legacy.py::anthropic_message_to_converse``.
    """
    content: list[ConverseContentBlock] = []
    for block in msg.content:
        match block:
            case AnthropicTextBlock():
                content.append(ConverseTextBlock(text=block.text))
            case AnthropicToolUseBlock():
                # Byte-parity: legacy substitutes empty dict when input is falsy
                # (None or empty). Preserving that quirk keeps hashes stable.
                tool_input: Any = block.input if block.input else {}
                content.append(
                    ConverseToolUseBlock(
                        toolUse=ConverseToolUse(
                            toolUseId=block.id,
                            name=block.name,
                            input=tool_input,
                        ),
                    ),
                )
            case AnthropicThinkingBlock():
                # Fold thinking into a text block to match Phase 1 behaviour.
                content.append(ConverseTextBlock(text=block.thinking))
            # AnthropicUnknownBlock: skipped silently.
    return ConverseResponse(
        output=ConverseOutput(
            message=ConverseAssistantMessage(role="assistant", content=content),
        ),
        stopReason=msg.stop_reason,
    )


def stream_to_converse(events: list[AnthropicStreamEvent]) -> ConverseResponse:
    """Walk Anthropic SSE events and reassemble into a Converse response.

    Malformed ``input_json_delta`` buffers produce ``{}`` as the tool_use
    input and emit a WARNING with the byte count (never the raw bytes —
    could contain PII).

    Byte-parity with ``_legacy.py::anthropic_stream_to_converse``.
    """
    content: list[ConverseContentBlock] = []
    slots: dict[int, dict[str, Any]] = {}  # index -> in-flight block state
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
                            "toolUseId": cb.id,
                            "name": cb.name,
                            "input_buf": "",
                        }
                    case AnthropicThinkingBlock():
                        slots[event.index] = {"_type": "thinking", "text": ""}
                    # AnthropicUnknownBlock: no slot allocated -> stop is a no-op.

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
                    content.append(ConverseTextBlock(text=slot.get("text", "")))
                elif block_type == "tool_use":
                    parsed_input: dict[str, Any] = {}
                    buf = slot.get("input_buf") or ""
                    if buf:
                        try:
                            parsed_input = cast("dict[str, Any]", json.loads(buf))
                        except json.JSONDecodeError:
                            # Length only — the buffer can hold tool arguments and
                            # we don't want those bytes in CloudWatch logs.
                            log.warning("tool_use input_json malformed (%d bytes)", len(buf))
                    content.append(
                        ConverseToolUseBlock(
                            toolUse=ConverseToolUse(
                                toolUseId=slot["toolUseId"],
                                name=slot["name"],
                                input=parsed_input,
                            ),
                        ),
                    )
                elif block_type == "thinking":
                    content.append(ConverseTextBlock(text=slot.get("text", "")))

            case AnthropicMessageDelta() if event.delta:
                raw_reason = event.delta.get("stop_reason")
                if isinstance(raw_reason, str) and raw_reason:
                    stop_reason = raw_reason

    return ConverseResponse(
        output=ConverseOutput(
            message=ConverseAssistantMessage(role="assistant", content=content),
        ),
        stopReason=stop_reason,
    )


def tools_to_converse_tool_config(
    tools: list[AnthropicToolDeclaration],
) -> ConverseToolConfig:
    """Convert Anthropic flat ``tools[]`` into a Converse ``toolConfig``.

    Byte-parity with ``_legacy.py::anthropic_tools_to_converse_tool_config``.
    Missing ``input_schema`` becomes ``{"json": {}}`` on the wire.
    """
    return ConverseToolConfig(
        tools=[
            ConverseTool(
                toolSpec=ConverseToolSpec(
                    name=t.name,
                    description=t.description,
                    # Construct via model_validate so pydantic honours the
                    # Python-side field name ``json_`` (aliased to ``json``
                    # on the wire because ``json`` is a Python builtin).
                    inputSchema=ConverseToolInputSchema.model_validate(
                        {"json_": t.input_schema or {}}
                    ),
                ),
            )
            for t in tools
        ],
    )


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


# --------------------------------------------------------------------------
# Phase 2 — direct-walk translates producing NormalizedInvocation
#
# These live alongside the legacy message_to_converse / stream_to_converse
# during the transition. Once mil_normalize (Chunk 8) rewires to consume
# these, Chunk 9 deletes the legacy functions and their fixtures.
# --------------------------------------------------------------------------


from ..normalized.types import (  # noqa: E402 — import here to keep Phase 1 section clean
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)
from .schema import (  # noqa: E402
    AnthropicRequestBody,
    AnthropicRequestContentBlock,
    AnthropicSystemBlock,
    AnthropicToolResultBlock,
)
from .stop_reasons import map as map_anthropic_stop_reason  # noqa: E402


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
    return NormalizedInvocationInput(messages=messages or None)


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
                out.append(NormalizedContent(
                    kind="tool_use",
                    tool_use_id=block.id,
                    tool_name=block.name,
                    tool_input=block.input if block.input else {},
                    tool_executor="client",
                ))
            case AnthropicThinkingBlock():
                out.append(NormalizedContent(kind="reasoning", text=block.thinking))
            case AnthropicToolResultBlock():
                out.append(NormalizedContent(
                    kind="tool_result",
                    tool_use_id=block.tool_use_id,
                    tool_output=block.content,
                    tool_is_error=block.is_error,
                    tool_executor="client",
                ))
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
                content.append(NormalizedContent(
                    kind="tool_use",
                    tool_use_id=block.id,
                    tool_name=block.name,
                    tool_input=block.input if block.input else {},
                    tool_executor="client",
                ))
            case AnthropicThinkingBlock():
                content.append(NormalizedContent(kind="reasoning", text=block.thinking))
            # AnthropicUnknownBlock: skipped silently.
    return NormalizedInvocationOutput(
        message=NormalizedMessage(role="assistant", content=content),
        stop_reason=map_anthropic_stop_reason(msg.stop_reason),
    )
