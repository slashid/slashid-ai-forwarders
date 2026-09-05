"""OpenTelemetry trace context extraction from MCP tool-result content.

MCP servers running mcp-gate-demo's ``CorrelationIdMiddleware`` echo the
OTel trace/span context back on every tool result. Wire shape:

    {"$opentelemetry": {"trace_id": "<32 hex>", "span_id": "<16 hex>"}}

Primary carrier is MCP ``structuredContent``. Success path preserves it —
reaches Bedrock Converse either as a native ``{json: {...}}`` block or, when
the client stringifies, a JSON-encoded ``{text: "..."}`` block. Error path:
some MCP clients (Claude Code on Bedrock) drop structured content entirely
and forward only the text message, so the middleware also embeds
``[trace_id=<32 hex> span_id=<16 hex>]`` as a trailing text marker.

This leaf module holds the extraction; both
``shared/events.py::_used_tools`` and
``shared/normalize/normalized/tool_results.py::extract_tool_result_files``
import from here. Leaf-only avoids the ``events.py`` ↔ ``tool_results.py``
cycle that would arise if OTel lived in either.
"""

from __future__ import annotations

import json
import re
from typing import Any

_OTEL_KEY = "$opentelemetry"
_TRACE_ID_HEX = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_SPAN_ID_HEX = re.compile(r"^[0-9a-f]{16}$", re.IGNORECASE)
_OTEL_MARKER = re.compile(
    r"\[trace_id=([0-9a-f]{32})\s+span_id=([0-9a-f]{16})\]",
    re.IGNORECASE,
)

_OtelCtx = tuple[str | None, str | None]  # (trace_id, span_id)


def _otel_from_dict(d: Any) -> _OtelCtx:
    """Pull ``$opentelemetry.{trace_id,span_id}`` from a decoded structured-content dict."""
    if not isinstance(d, dict):
        return (None, None)
    otel = d.get(_OTEL_KEY)
    if not isinstance(otel, dict):
        return (None, None)
    raw_t = otel.get("trace_id")
    raw_s = otel.get("span_id")
    trace_id = raw_t.lower() if isinstance(raw_t, str) and _TRACE_ID_HEX.match(raw_t) else None
    span_id = raw_s.lower() if isinstance(raw_s, str) and _SPAN_ID_HEX.match(raw_s) else None
    return (trace_id, span_id)


def _otel_from_text(text: str) -> _OtelCtx:
    """Pull OTel context from a text block via either a JSON envelope or the
    ``[trace_id=<hex> span_id=<hex>]`` marker (the fallback carrier used on
    error paths where the client drops structuredContent).
    """
    stripped = text.strip()
    if stripped and stripped[0] in "{[":
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass
        else:
            ctx = _otel_from_dict(parsed)
            if ctx[0]:
                return ctx
    m = _OTEL_MARKER.search(text)
    if m:
        return (m.group(1).lower(), m.group(2).lower())
    return (None, None)


def extract_otel(content: Any) -> _OtelCtx:
    """Pull the MCP ``$opentelemetry`` context from a tool_result payload.

    Handles both the Anthropic tool_result shape (string or list of blocks)
    and the Converse toolResult shape (list of ``{text}`` / ``{json}`` /
    etc. blocks). Returns ``(None, None)`` when the marker is absent.
    """
    if content is None:
        return (None, None)
    if isinstance(content, str):
        return _otel_from_text(content)
    if isinstance(content, dict):
        return _otel_from_dict(content)
    if not isinstance(content, list):
        return (None, None)
    for block in content:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("json"), dict):
            ctx = _otel_from_dict(block["json"])
            if ctx[0]:
                return ctx
        text = block.get("text")
        if isinstance(text, str):
            ctx = _otel_from_text(text)
            if ctx[0]:
                return ctx
    return (None, None)
