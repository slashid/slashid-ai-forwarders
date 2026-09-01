"""Normalize Bedrock MIL records to the Converse shape via typed dispatch.

Table-driven: each entry in ``_FORMATS`` pairs a ``TypeAdapter`` with a
pure translate function that returns a ``ConverseResponse``. Optional
``on_parse`` callbacks handle envelope-specific side effects (Anthropic
token backfill into MIL top-level fields). First-match-wins ordering is
safe because the format shapes are structurally exclusive (Anthropic
message is a dict-with-`type:"message"`; Anthropic stream is a list;
Converse response is a dict-with-`output.message.role`).

The vendor payload logic (Anthropic Messages ↔ Converse transforms)
lives in :mod:`slashid_ai_forwarder_core.normalize.anthropic.normalize`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    extract_stream_usage,
    message_to_converse,
    stream_to_converse,
    tools_to_converse_tool_config,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicMessage,
    AnthropicStreamEvent,
    AnthropicToolDeclaration,
    AnthropicUsage,
)
from slashid_ai_forwarder_core.normalize.converse.schema import ConverseResponse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Format[T]:
    """One row in the MIL format-dispatch table."""

    name: str
    adapter: TypeAdapter[T]
    translate: Callable[[T], ConverseResponse]
    on_parse: Callable[[dict[str, Any], T], None] | None = None


def _on_anthropic_message_parse(rec: dict[str, Any], msg: AnthropicMessage) -> None:
    """Anthropic-message envelope side effects: rewrite input tools + backfill tokens."""
    _rewrite_input_tools(rec)
    _backfill_tokens_from_usage(rec, msg.usage)


def _on_anthropic_stream_parse(
    rec: dict[str, Any],
    events: list[AnthropicStreamEvent],
) -> None:
    """Anthropic-stream envelope side effects: rewrite input tools + backfill tokens."""
    _rewrite_input_tools(rec)
    _backfill_tokens_from_usage(rec, extract_stream_usage(events))


_FORMATS: list[_Format] = [  # type: ignore[type-arg]
    _Format(
        name="anthropic-message",
        adapter=TypeAdapter(AnthropicMessage),
        translate=message_to_converse,
        on_parse=_on_anthropic_message_parse,
    ),
    _Format(
        name="anthropic-stream",
        adapter=TypeAdapter(list[AnthropicStreamEvent]),
        translate=stream_to_converse,
        on_parse=_on_anthropic_stream_parse,
    ),
    _Format(
        name="bedrock-converse",
        adapter=TypeAdapter(ConverseResponse),
        translate=lambda r: r,  # identity — already Converse; no envelope side effects
    ),
]


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Dispatch on the record's output body shape and rewrite in place.

    Sets ``record["_parsed_as"]`` to the matching format name, or
    ``"unknown"`` on fallthrough. ``build_event`` reads this to populate
    the wire field.

    Note: request-side ``_rewrite_input_tools`` is called from the
    Anthropic ``on_parse`` callbacks (family-scoped) — not unconditionally
    up-front — so non-Anthropic output shapes (Converse, unknown) leave
    the input body untouched.
    """
    out = (record.get("output") or {}).get("outputBodyJson")
    for fmt in _FORMATS:
        try:
            parsed = fmt.adapter.validate_python(out)
        except ValidationError:
            continue
        if fmt.on_parse is not None:
            fmt.on_parse(record, parsed)
        canonical = fmt.translate(parsed)
        record["output"]["outputBodyJson"] = canonical.model_dump(
            mode="json",
            exclude_none=True,
        )
        record["_parsed_as"] = fmt.name
        log.debug("normalized record as %s", fmt.name)
        return record
    log.warning(
        "unrecognized MIL body shape for model=%s request_id=%s",
        record.get("modelId"),
        record.get("requestId"),
    )
    record["_parsed_as"] = "unknown"
    return record


def _rewrite_input_tools(record: dict[str, Any]) -> None:
    """Rewrite Anthropic-side body.tools[] → body.toolConfig via the shared helper.

    Bespoke function in 1.1 — the only request-side transform we need.
    When Phase 2 grows more request-side rewrites, migrate to a parallel
    ``_INPUT_FORMATS`` table with the same shape as ``_FORMATS``.
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
    """Copy Anthropic body.usage counts to MIL top-level fields when missing.

    Bedrock MIL populates inputTokenCount/outputTokenCount at the record
    top level for non-streaming Anthropic InvokeModel responses, but does
    NOT populate cacheReadInputTokenCount / cacheWriteInputTokenCount.
    The counts live inside body.usage; backfill before the body is
    replaced by translate. Idempotent — only sets fields currently None.
    """
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
