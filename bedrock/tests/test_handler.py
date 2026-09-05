"""Handler-level tests against synthetic CW Logs payloads."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
from typing import Any

import pytest

from slashid_bedrock_forwarder import handler
from slashid_bedrock_forwarder.config import Config
from slashid_bedrock_forwarder.handler import (
    CWLogsEvent,
    _decode_cw_payload,
    _records_from_payload,
    _run,
)


def _wrap_cw_event(payload: dict[str, Any]) -> dict[str, Any]:
    compressed = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(compressed).decode()}}


def _mil_message(**overrides: Any) -> str:
    record: dict[str, Any] = {
        "requestId": "req-1",
        "timestamp": "2026-06-01T12:00:00Z",
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "accountId": "123456789012",
        "identity": {"arn": "arn:aws:iam::123456789012:user/alice"},
        "input": {"inputTokenCount": 100},
        "output": {"outputTokenCount": 50, "outputBodyJson": {"stopReason": "end_turn"}},
    }
    record.update(overrides)
    return json.dumps(record)


def test_decode_cw_payload_roundtrip() -> None:
    raw_event = _wrap_cw_event(
        {
            "messageType": "DATA_MESSAGE",
            "logEvents": [{"id": "1", "timestamp": 0, "message": _mil_message()}],
        }
    )
    decoded = _decode_cw_payload(CWLogsEvent.model_validate(raw_event))
    assert decoded.messageType == "DATA_MESSAGE"
    assert len(decoded.logEvents) == 1


def test_records_from_payload_skips_control_messages() -> None:
    raw_event = _wrap_cw_event(
        {
            "messageType": "CONTROL_MESSAGE",
            "logEvents": [{"id": "1", "timestamp": 0, "message": _mil_message()}],
        }
    )
    payload = _decode_cw_payload(CWLogsEvent.model_validate(raw_event))
    assert _records_from_payload(payload) == []


def test_records_from_payload_extracts_data_messages() -> None:
    raw_event = _wrap_cw_event(
        {
            "messageType": "DATA_MESSAGE",
            "logEvents": [
                {"id": "1", "timestamp": 0, "message": _mil_message()},
                {"id": "2", "timestamp": 0, "message": "not json"},
            ],
        }
    )
    payload = _decode_cw_payload(CWLogsEvent.model_validate(raw_event))
    records = _records_from_payload(payload)
    assert len(records) == 1
    assert records[0]["requestId"] == "req-1"


def test_lambda_handler_empty_payload_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty payload short-circuits before _run, so the env vars don't need to be set.
    monkeypatch.setattr(handler, "load_config", lambda: object())

    cw_event = _wrap_cw_event({"messageType": "CONTROL_MESSAGE", "logEvents": []})
    result = handler.lambda_handler(cw_event, None)
    assert result == {"events_pushed": 0, "records_seen": 0}


def test_run_normalizes_after_offload_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offloaded Anthropic-shape bodies must be normalized after S3 fetch.

    Regression test: prior to the fix, `normalize_record` ran in
    `_records_from_payload` against a `null` body (offloads land later),
    so Claude Code's large `tools[]` payloads never reached the Converse
    shape and `_available_tools` emitted empty arrays.
    """
    anthropic_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [
            {
                "name": "WebFetch",
                "description": "Fetch a URL",
                "input_schema": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            }
        ],
    }
    record: dict[str, Any] = {
        "requestId": "req-offload",
        "timestamp": "2026-06-30T01:00:00Z",
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "region": "us-east-2",
        "accountId": "123456789012",
        "identity": {"arn": "arn:aws:iam::123456789012:user/alice"},
        "input": {
            "inputTokenCount": 1000,
            "inputBodyJson": None,
            "inputBodyS3Path": "s3://bucket/key.json.gz",
        },
        "output": {
            "outputTokenCount": 5,
            # Anthropic non-streaming response shape — matches the input's
            # Anthropic Messages family, which is what triggers input-tool
            # normalization in the new dispatch model.
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "tool_use",
            },
        },
    }

    async def fake_resolve(records: list[dict[str, Any]]) -> None:
        for r in records:
            inp = r.get("input") or {}
            if inp.get("inputBodyJson") is None and inp.get("inputBodyS3Path"):
                inp["inputBodyJson"] = anthropic_body

    captured: dict[str, Any] = {}

    async def fake_push(
        _client: Any,
        events: list[Any],
        **_kw: Any,
    ) -> int:
        captured["events"] = events
        return len(events)

    monkeypatch.setattr(handler, "resolve_offloaded_bodies", fake_resolve)
    monkeypatch.setattr(handler, "push_invocations", fake_push)

    config = Config(endpoint="https://api.slashid.com", push_token="t" * 32)
    asyncio.run(_run([record], config))

    assert "events" in captured, "push_invocations was not called"
    assert len(captured["events"]) == 1
    ev = captured["events"][0]
    assert ev.available_tools is not None, (
        "available_tools is None — normalize_record did not run after offload resolution"
    )
    assert len(ev.available_tools) == 1
    assert ev.available_tools[0].name == "WebFetch"
    assert ev.available_tool_servers is not None
    assert len(ev.available_tool_servers) == 1


def test_run_populates_accessed_files_from_both_extractors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: _run composes shared Converse attachment extraction (inside
    ``to_normalized_invocation``) + shared ``finalize``; both paths'
    AIAccessedFile entries land on the wire event. Also verifies the
    cross-path (name, sha256) dedup — a file surfaced by both extractors
    appears once."""
    content = b"shared content"
    b64 = base64.b64encode(content).decode()
    tool_use_id = "tu_dup"
    # Layout so both extractors emit against the fresh region:
    # - msg[0]: assistant tool_use (last assistant → fresh region is msg[1:])
    # - msg[1]: user with the document attachment AND the tool_result for the
    #   same file. Attachment extractor emits one entry (name, sha256=X);
    #   tool_result extractor emits (name, sha256=X); finalize dedups.
    converse_body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": tool_use_id,
                            "name": "Read",
                            "input": {"file_path": "shared.txt"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "name": "shared.txt",
                            "format": "txt",
                            "source": {"bytes": b64},
                        }
                    },
                    {
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "status": "success",
                            "content": [{"text": content.decode()}],
                        }
                    },
                ],
            },
        ]
    }
    # Valid Converse response so the record matches the ``bedrock-converse``
    # format — attachment extraction now rides inside dispatch and needs a
    # valid vendor match.
    converse_response = {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
    }
    record: dict[str, Any] = {
        "requestId": "req-dedup",
        "timestamp": "2026-06-30T02:00:00Z",
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "region": "us-east-2",
        "accountId": "123456789012",
        "identity": {"arn": "arn:aws:iam::123456789012:user/alice"},
        "input": {"inputTokenCount": 10, "inputBodyJson": converse_body},
        "output": {"outputTokenCount": 5, "outputBodyJson": converse_response},
    }

    async def fake_resolve(records: list[dict[str, Any]]) -> None:
        pass

    captured: dict[str, Any] = {}

    async def fake_push(
        _client: Any,
        events: list[Any],
        **_kw: Any,
    ) -> int:
        captured["events"] = events
        return len(events)

    monkeypatch.setattr(handler, "resolve_offloaded_bodies", fake_resolve)
    monkeypatch.setattr(handler, "push_invocations", fake_push)

    config = Config(endpoint="https://api.slashid.com", push_token="t" * 32)
    asyncio.run(_run([record], config))

    assert "events" in captured
    assert len(captured["events"]) == 1
    ev = captured["events"][0]
    assert ev.accessed_files is not None
    assert len(ev.accessed_files) == 1  # deduped by (name, content_hash)
    f = ev.accessed_files[0]
    assert f.name == "shared.txt"
