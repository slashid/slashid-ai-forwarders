"""Table-driven MIL record dispatcher producing NormalizedInvocation.

Each ``_FORMATS`` entry pairs a request-side TypeAdapter with a
response-side TypeAdapter and a joint ``to_invocation`` translate.
BOTH must validate for a format to match — this makes ``parsed_as``
honestly reflect whether we understood the record.

``on_parse`` callbacks fire after both parses succeed and receive
``(record, request, response)`` — extended from Phase 1.1's
``(record, response)`` so callbacks can read request-side data (e.g.
tools_declared for wire ``available_tools``). Today only the token
backfill is used; ``request`` is present for future extensions.

``normalize_record(record) -> NormalizedInvocation`` returns the
canonical shape (or an empty ``NormalizedInvocation()`` on fallthrough)
and additionally sets ``record["_parsed_as"]`` so ``build_event`` can
read it. ``_rewrite_input_tools`` still mutates the record's inputBody
so ``events._available_tools(record)`` continues to work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    extract_stream_usage,
    message_to_normalized_invocation,
    stream_to_normalized_invocation,
    tools_to_converse_tool_config,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicStreamEvent,
    AnthropicToolDeclaration,
    AnthropicUsage,
)
from slashid_ai_forwarder_core.normalize.converse.normalize import (
    to_normalized_invocation as converse_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.converse.schema import (
    ConverseRequestBody,
    ConverseResponse,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Format[TIn, TOut]:
    """One row in the MIL format-dispatch table.

    ``on_parse`` fires after both request and response parse successfully
    and before ``to_invocation`` — mutation hook for envelope-level side
    effects (token backfill, input-tool rewriting).
    """

    name: str
    request_adapter: TypeAdapter[TIn]
    response_adapter: TypeAdapter[TOut]
    to_invocation: Callable[[TIn, TOut], NormalizedInvocation]
    on_parse: Callable[[dict[str, Any], TIn, TOut], None] | None = None


def _on_anthropic_message_parse(
    rec: dict[str, Any],
    request: AnthropicRequestBody,
    msg: AnthropicMessage,
) -> None:
    """Anthropic-message envelope side effects: rewrite input tools + backfill tokens.

    ``request`` is available but unused today — future work (e.g. populating
    ``available_tools`` from `request.tools`) will read it. Keep the arg.
    """
    del request  # explicitly unused, for signature-conformance
    _rewrite_input_tools(rec)
    _backfill_tokens_from_usage(rec, msg.usage)


def _on_anthropic_stream_parse(
    rec: dict[str, Any],
    request: AnthropicRequestBody,
    events: list[AnthropicStreamEvent],
) -> None:
    """Anthropic-stream envelope side effects: rewrite input tools + backfill tokens."""
    del request
    _rewrite_input_tools(rec)
    _backfill_tokens_from_usage(rec, extract_stream_usage(events))


_FORMATS: list[_Format] = [  # type: ignore[type-arg]  # heterogeneous [TIn, TOut] pairs
    _Format(
        name="anthropic-message",
        request_adapter=TypeAdapter(AnthropicRequestBody),
        response_adapter=TypeAdapter(AnthropicMessage),
        to_invocation=message_to_normalized_invocation,
        on_parse=_on_anthropic_message_parse,
    ),
    _Format(
        name="anthropic-stream",
        request_adapter=TypeAdapter(AnthropicRequestBody),
        response_adapter=TypeAdapter(list[AnthropicStreamEvent]),
        to_invocation=stream_to_normalized_invocation,
        on_parse=_on_anthropic_stream_parse,
    ),
    _Format(
        name="bedrock-converse",
        request_adapter=TypeAdapter(ConverseRequestBody),
        response_adapter=TypeAdapter(ConverseResponse),
        to_invocation=converse_to_normalized_invocation,
    ),
]


def normalize_record(record: dict[str, Any]) -> NormalizedInvocation:
    """Dispatch on the record's input+output body shapes → NormalizedInvocation.

    Sets ``record["_parsed_as"]`` to the matching format name, or
    ``"unknown"`` on fallthrough — ``build_event`` reads this to populate
    the wire field.

    Both request and response must validate for a format to match. If either
    fails (e.g. S3 offload never landed and inputBodyJson is None, or the
    vendor sent a shape we haven't taught the schema yet), the dispatcher
    falls through and returns ``NormalizedInvocation()`` — envelope-side
    fields still emit (identity, model, tokens), only semantic detail is
    dropped.
    """
    in_body = (record.get("input") or {}).get("inputBodyJson")
    out_body = (record.get("output") or {}).get("outputBodyJson")

    for fmt in _FORMATS:
        try:
            parsed_out = fmt.response_adapter.validate_python(out_body)
            parsed_in = fmt.request_adapter.validate_python(in_body)
        except ValidationError:
            continue
        if fmt.on_parse is not None:
            fmt.on_parse(record, parsed_in, parsed_out)
        record["_parsed_as"] = fmt.name
        log.debug("normalized record as %s", fmt.name)
        return fmt.to_invocation(parsed_in, parsed_out)

    log.warning(
        "unrecognized MIL body shape for model=%s request_id=%s",
        record.get("modelId"),
        record.get("requestId"),
    )
    record["_parsed_as"] = "unknown"
    return NormalizedInvocation()


def _rewrite_input_tools(record: dict[str, Any]) -> None:
    """Rewrite Anthropic-side body.tools[] → body.toolConfig via the shared helper.

    Kept from Phase 1.1 — ``events._available_tools(record)`` still reads
    Converse-shape toolConfig off the record's inputBodyJson. When
    ``available_tools`` migrates to be built from ``NormalizedInvocation``
    (future phase), this becomes deletable.
    """
    body = (record.get("input") or {}).get("inputBodyJson")
    if not isinstance(body, dict) or "toolConfig" in body:
        return
    raw_tools = body.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        return
    tools = [AnthropicToolDeclaration.model_validate(t) for t in raw_tools if isinstance(t, dict)]
    if not tools:
        return
    body["toolConfig"] = tools_to_converse_tool_config(tools).model_dump(
        by_alias=True,
        exclude_none=True,
    )


def _backfill_tokens_from_usage(
    record: dict[str, Any],
    usage: AnthropicUsage | None,
) -> None:
    """Copy Anthropic body.usage counts to MIL top-level fields when missing."""
    if usage is None:
        return
    inp = record.setdefault("input", {})
    out = record.setdefault("output", {})
    _set_if_absent(inp, "inputTokenCount", usage.input_tokens)
    _set_if_absent(out, "outputTokenCount", usage.output_tokens)
    _set_if_absent(inp, "cacheReadInputTokenCount", usage.cache_read_input_tokens)
    _set_if_absent(inp, "cacheWriteInputTokenCount", usage.cache_creation_input_tokens)


def _set_if_absent(container: dict[str, Any], key: str, value: int | None) -> None:
    if container.get(key) is None and isinstance(value, int):
        container[key] = value
