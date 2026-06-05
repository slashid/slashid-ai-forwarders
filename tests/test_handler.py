"""Handler-level tests against synthetic CW Logs payloads."""

from __future__ import annotations

import base64
import gzip
import json
from typing import Any

import pytest

from slashid_bedrock_forwarder import handler
from slashid_bedrock_forwarder.handler import (
    CWLogsEvent,
    _decode_cw_payload,
    _records_from_payload,
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


def test_records_from_payload_extracts_and_normalizes() -> None:
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
