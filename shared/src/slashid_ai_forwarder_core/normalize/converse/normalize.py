"""Bedrock Converse request+response → canonical NormalizedInvocation.

Public API: ``to_normalized_invocation(request, response)`` — a joint
translate that walks both halves into their canonical shape. Internal
``_to_input`` / ``_to_output`` helpers are unexported (underscored);
callers should stick to the joint entry point so system-prompt handling
and other cross-half invariants stay centralized.
"""

from __future__ import annotations

from ..normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)
from .schema import (
    ConverseContentBlock,
    ConverseReasoningBlock,
    ConverseRequestBody,
    ConverseRequestContentBlock,
    ConverseResponse,
    ConverseTextBlock,
    ConverseToolResultBlock,
    ConverseToolUseBlock,
)
from .stop_reasons import map as map_converse_stop_reason


def to_normalized_invocation(
    request: ConverseRequestBody,
    response: ConverseResponse,
) -> NormalizedInvocation:
    """Map a Converse request+response pair → canonical NormalizedInvocation."""
    return NormalizedInvocation(
        input=_to_input(request),
        output=_to_output(response),
    )


def _to_input(request: ConverseRequestBody) -> NormalizedInvocationInput:
    """Walk request body → NormalizedInvocationInput.

    Prepends any ``system`` content as an index-0 ``NormalizedMessage`` with
    ``role="system"`` (per the "System messages" design convention). Tool
    declarations and tool servers are left ``None`` for now — Chunk 8 wires
    them up when mil_normalize starts consuming NormalizedInvocation for
    the ``available_tools`` list; today they're already derived envelope-
    side via ``events._available_tools(record)`` reading the raw record.
    """
    messages: list[NormalizedMessage] = []
    if request.system:
        system_text = "".join(block.text or "" for block in request.system)
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


def _translate_request_content(
    blocks: list[ConverseRequestContentBlock],
) -> list[NormalizedContent]:
    out: list[NormalizedContent] = []
    for block in blocks:
        match block:
            case ConverseTextBlock():
                out.append(NormalizedContent(kind="text", text=block.text))
            case ConverseToolUseBlock():
                tu = block.toolUse
                out.append(NormalizedContent(
                    kind="tool_use",
                    tool_use_id=tu.toolUseId,
                    tool_name=tu.name,
                    tool_input=tu.input if tu.input else {},
                    tool_executor="client",
                ))
            case ConverseToolResultBlock():
                tr = block.toolResult
                out.append(NormalizedContent(
                    kind="tool_result",
                    tool_use_id=tr.toolUseId,
                    tool_output=tr.content,
                    tool_is_error=tr.status == "error",
                    tool_executor="client",
                ))
            case ConverseReasoningBlock():
                rc = block.reasoningContent
                text = (rc.reasoningText.text if rc.reasoningText else "") or ""
                out.append(NormalizedContent(kind="reasoning", text=text))
            # ConverseUnknownBlock: silently skipped (matches Phase 1.1
            # behaviour for unmodeled block types).
    return out


def _to_output(response: ConverseResponse) -> NormalizedInvocationOutput:
    """Walk response envelope → NormalizedInvocationOutput.

    Tool_use blocks are marked ``tool_executor="client"`` — Bedrock
    Converse has no server-side tool concept, so all tool use is
    client-executed. Unknown block types are skipped silently.
    """
    message = response.output.message
    content = _translate_response_content(message.content)
    return NormalizedInvocationOutput(
        message=NormalizedMessage(role=message.role, content=content),
        stop_reason=map_converse_stop_reason(response.stopReason),
    )


def _translate_response_content(
    blocks: list[ConverseContentBlock],
) -> list[NormalizedContent]:
    out: list[NormalizedContent] = []
    for block in blocks:
        match block:
            case ConverseTextBlock():
                out.append(NormalizedContent(kind="text", text=block.text))
            case ConverseToolUseBlock():
                tu = block.toolUse
                out.append(NormalizedContent(
                    kind="tool_use",
                    tool_use_id=tu.toolUseId,
                    tool_name=tu.name,
                    tool_input=tu.input if tu.input else {},
                    tool_executor="client",
                ))
            case ConverseReasoningBlock():
                rc = block.reasoningContent
                text = (rc.reasoningText.text if rc.reasoningText else "") or ""
                out.append(NormalizedContent(kind="reasoning", text=text))
            # ConverseUnknownBlock: silently skipped.
    return out


# --------------------------------------------------------------------------
# Supported test helper — bridges a raw Converse-shape MIL record dict into
# NormalizedInvocation with best-effort validation on each side. The
# production Bedrock forwarder uses ``mil_normalize.normalize_record``
# directly (tighter TIn+TOut contract). This helper stays available for
# shared-package tests, audit-envelope replay, and future backfill scripts
# that hold raw Converse-shape dicts.
# --------------------------------------------------------------------------

from pydantic import TypeAdapter, ValidationError  # noqa: E402


_REQUEST_ADAPTER = TypeAdapter(ConverseRequestBody)
_RESPONSE_ADAPTER = TypeAdapter(ConverseResponse)


def converse_dict_to_normalized(record: dict) -> NormalizedInvocation:  # type: ignore[type-arg]
    """Convenience adapter: MIL/Converse-dict record → NormalizedInvocation.

    Best-effort on each side — validation failure falls back to the empty
    default rather than raising. Useful for callers that hold raw
    Converse-shape dicts (audit-envelope replay, backfill scripts,
    test helpers).

    Production Bedrock forwarder uses ``mil_normalize.normalize_record``
    directly, which enforces the tighter TIn+TOut contract.
    """
    in_body = (record.get("input") or {}).get("inputBodyJson")
    out_body = (record.get("output") or {}).get("outputBodyJson")

    input_side: NormalizedInvocationInput
    try:
        parsed_in = _REQUEST_ADAPTER.validate_python(in_body)
    except ValidationError:
        input_side = NormalizedInvocationInput()
    else:
        input_side = _to_input(parsed_in)

    output_side: NormalizedInvocationOutput
    try:
        parsed_out = _RESPONSE_ADAPTER.validate_python(out_body)
    except ValidationError:
        output_side = NormalizedInvocationOutput()
    else:
        output_side = _to_output(parsed_out)

    return NormalizedInvocation(input=input_side, output=output_side)
