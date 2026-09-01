"""Normalize Bedrock MIL records to the Converse shape.

Envelope glue for Bedrock's Model Invocation Logging: unwraps
``record["input"]["inputBodyJson"]`` / ``record["output"]["outputBodyJson"]``,
dispatches on payload shape, calls the shared Anthropic → Converse pure
functions, and writes results back into the envelope. Already-Converse
records pass through unchanged.

The vendor-shape logic (Anthropic Messages ↔ Converse transforms) lives
in :mod:`slashid_ai_forwarder_core.normalize.anthropic` — Vertex's
``rawPredict`` path on Anthropic reuses it verbatim, with a Vertex-shaped
envelope glue file replacing this one.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from slashid_ai_forwarder_core.normalize.anthropic import (
    anthropic_message_to_converse,
    anthropic_stream_to_converse,
    anthropic_tools_to_converse_tool_config,
    extract_anthropic_stream_usage,
    looks_like_anthropic_message,
    looks_like_anthropic_stream,
)

log = logging.getLogger(__name__)


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Dispatch on the record's API family and rewrite in place to Converse shape.

    Each family-normalizer owns the full input+output rewrite for its
    shape, because the caller's API family determines both. Unrecognized
    shapes (Converse, other-vendor streams) are left untouched.
    """
    out = (record.get("output") or {}).get("outputBodyJson")
    if looks_like_anthropic_stream(out):
        _normalize_anthropic_stream(record, cast("list[Any]", out))
    elif looks_like_anthropic_message(out):
        _normalize_anthropic_message(record, cast("dict[str, Any]", out))
    return record


def _normalize_anthropic_stream(record: dict[str, Any], events: list[Any]) -> None:
    """Rewrite an Anthropic streaming record in place to Converse shape.

    Precondition: dispatched to only after ``looks_like_anthropic_stream``
    matched, which is the only guarantor that
    ``record["output"]["outputBodyJson"]`` is a list — bracket access
    below is safe post-detection.
    """
    _rewrite_input_tools(record)
    _backfill_tokens_from_body_usage(record, extract_anthropic_stream_usage(events))
    record["output"]["outputBodyJson"] = anthropic_stream_to_converse(events)


def _normalize_anthropic_message(record: dict[str, Any], body: dict[str, Any]) -> None:
    """Rewrite an Anthropic non-streaming record in place to Converse shape.

    Precondition: dispatched to only after ``looks_like_anthropic_message``
    matched, which is the only guarantor that
    ``record["output"]["outputBodyJson"]`` is a dict — bracket access
    below is safe post-detection.
    """
    _rewrite_input_tools(record)
    _backfill_tokens_from_body_usage(record, body.get("usage"))
    record["output"]["outputBodyJson"] = anthropic_message_to_converse(body)


def _rewrite_input_tools(record: dict[str, Any]) -> None:
    """Rewrite request-side ``body.tools[]`` → ``body.toolConfig`` via the shared helper.

    Idempotent — leaves the body alone when ``toolConfig`` is already
    present or when there are no Anthropic-flat tools to convert.
    """
    input_body = (record.get("input") or {}).get("inputBodyJson")
    if not isinstance(input_body, dict) or "toolConfig" in input_body:
        return
    tools = input_body.get("tools")
    if not isinstance(tools, list):
        return
    tool_config = anthropic_tools_to_converse_tool_config(tools)
    if tool_config:
        input_body["toolConfig"] = tool_config


def _backfill_tokens_from_body_usage(record: dict[str, Any], usage: Any) -> None:
    """Copy Anthropic ``body.usage`` counts to MIL top-level fields when missing.

    Bedrock MIL populates ``input.inputTokenCount`` and ``output.outputTokenCount``
    at the record top level for non-streaming Anthropic InvokeModel responses,
    but does NOT populate ``input.cacheReadInputTokenCount`` or
    ``cacheWriteInputTokenCount``. The counts are always inside ``body.usage``,
    so backfill before we discard the body during reconstruction. Downstream
    (``build_event.tokens``) then reads all four fields uniformly at the top
    level regardless of API family.

    Idempotent — only sets fields that are currently None. MIL wins when set.
    """
    if not isinstance(usage, dict):
        return
    inp = record.setdefault("input", {})
    out = record.setdefault("output", {})
    _set_if_absent(inp, "inputTokenCount", usage.get("input_tokens"))
    _set_if_absent(out, "outputTokenCount", usage.get("output_tokens"))
    _set_if_absent(inp, "cacheReadInputTokenCount", usage.get("cache_read_input_tokens"))
    _set_if_absent(inp, "cacheWriteInputTokenCount", usage.get("cache_creation_input_tokens"))


def _set_if_absent(container: dict[str, Any], key: str, value: Any) -> None:
    """Set ``container[key] = value`` only when the key is missing / None and
    the value is a non-None int (the shape Anthropic uses for token counts)."""
    if container.get(key) is None and isinstance(value, int):
        container[key] = value
