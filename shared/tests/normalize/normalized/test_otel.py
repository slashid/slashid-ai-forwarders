"""Unit tests for extract_otel — pulls OTel context from MCP tool_result content."""

from __future__ import annotations

import json

from slashid_ai_forwarder_core.normalize.normalized.otel import extract_otel

_TRACE = "a" * 32
_SPAN = "1" * 16


def test_extract_otel_none() -> None:
    assert extract_otel(None) == (None, None)


def test_extract_otel_empty_string() -> None:
    assert extract_otel("") == (None, None)


def test_extract_otel_json_string_envelope() -> None:
    envelope = json.dumps({"$opentelemetry": {"trace_id": _TRACE, "span_id": _SPAN}})
    assert extract_otel(envelope) == (_TRACE, _SPAN)


def test_extract_otel_text_marker() -> None:
    text = f"error occurred\n[trace_id={_TRACE} span_id={_SPAN}]"
    assert extract_otel(text) == (_TRACE, _SPAN)


def test_extract_otel_converse_json_block() -> None:
    """Converse content array with a {json: {...}} block."""
    content = [{"json": {"$opentelemetry": {"trace_id": _TRACE, "span_id": _SPAN}}}]
    assert extract_otel(content) == (_TRACE, _SPAN)


def test_extract_otel_converse_text_block_with_json_envelope() -> None:
    """Some clients stringify structuredContent as a {text: '<json>'} block."""
    envelope = json.dumps({"$opentelemetry": {"trace_id": _TRACE, "span_id": _SPAN}})
    content = [{"text": envelope}]
    assert extract_otel(content) == (_TRACE, _SPAN)


def test_extract_otel_converse_text_block_with_marker() -> None:
    """Error paths: text block with the [trace_id=… span_id=…] marker."""
    text = f"tool failed [trace_id={_TRACE} span_id={_SPAN}]"
    content = [{"text": text}]
    assert extract_otel(content) == (_TRACE, _SPAN)


def test_extract_otel_ignores_invalid_hex_lengths() -> None:
    """trace_id must be exactly 32 hex chars, span_id exactly 16."""
    envelope = json.dumps({"$opentelemetry": {"trace_id": "abc", "span_id": "def"}})
    assert extract_otel(envelope) == (None, None)


def test_extract_otel_lowercases_hex() -> None:
    """Uppercase hex normalises to lowercase."""
    envelope = json.dumps(
        {"$opentelemetry": {"trace_id": _TRACE.upper(), "span_id": _SPAN.upper()}}
    )
    assert extract_otel(envelope) == (_TRACE, _SPAN)


def test_extract_otel_missing_key() -> None:
    """Structured content without the $opentelemetry key returns (None, None)."""
    content = [{"json": {"flow": "gate_svid"}}]
    assert extract_otel(content) == (None, None)
