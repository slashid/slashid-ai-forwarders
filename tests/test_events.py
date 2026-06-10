"""Unit tests for the pure transformation logic in `events`."""

from __future__ import annotations

from typing import Any

import pytest

from slashid_bedrock_forwarder.events import (
    AIInvocationObservedV1,
    build_event,
    parse_tool_name,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__claude_ai_Excalidraw__create_view", ("create_view", "claude_ai_Excalidraw", "mcp")),
        ("git__status", ("status", "git", "runtime")),
        ("Bash", ("Bash", "builtin", "runtime")),
        ("Read", ("Read", "builtin", "runtime")),
        ("mcp__foo", ("foo", "builtin", "runtime")),
    ],
)
def test_parse_tool_name(name: str, expected: tuple[str, str, str]) -> None:
    assert parse_tool_name(name) == expected


def _mil_record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "requestId": "req-1",
        "timestamp": "2026-06-01T12:00:00Z",
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "accountId": "123456789012",
        "identity": {"arn": "arn:aws:iam::123456789012:user/alice"},
        "input": {"inputTokenCount": 100, "cacheReadInputTokenCount": 5},
        "output": {"outputTokenCount": 50, "outputBodyJson": {"stopReason": "end_turn"}},
    }
    base.update(overrides)
    return base


def test_build_event_minimal() -> None:
    event = build_event(_mil_record(), identity_source_type="manual_import")
    assert event is not None
    assert isinstance(event, AIInvocationObservedV1)
    assert event.request_id == "req-1"
    assert event.identifier_from_source == "arn:aws:iam::123456789012:user/alice"
    assert event.model.id == "us.anthropic.claude-sonnet-4-6"
    assert event.tokens.input == 100
    assert event.tokens.output == 50
    assert event.tokens.cache_read == 5
    assert event.tokens.cache_write == 0
    assert event.stop_reason == "end_turn"
    # org_id and connection_id stay None — the server derives them from the push token.
    assert event.org_id is None
    assert event.connection_id is None
    # Optional fields stay None when no tools are present.
    assert event.available_tool_servers is None
    assert event.available_tools is None
    assert event.used_tool_ids is None


def test_build_event_skips_records_without_request_id() -> None:
    record = _mil_record()
    del record["requestId"]
    assert build_event(record, identity_source_type="manual_import") is None


def test_build_event_with_tools_and_used_ids() -> None:
    record = _mil_record(
        input={
            "inputTokenCount": 100,
            "inputBodyJson": {
                "toolConfig": {
                    "tools": [
                        {
                            "toolSpec": {
                                "name": "mcp__excalidraw__create_view",
                                "description": "make a drawing",
                                "inputSchema": {"json": {"type": "object"}},
                            }
                        },
                        {"toolSpec": {"name": "Bash", "description": "shell"}},
                    ]
                }
            },
        },
        output={
            "outputTokenCount": 50,
            "outputBodyJson": {
                "stopReason": "tool_use",
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "name": "mcp__excalidraw__create_view",
                                    "input": {},
                                }
                            }
                        ]
                    }
                },
            },
        },
    )
    event = build_event(record, identity_source_type="manual_import")
    assert event is not None
    assert event.available_tool_servers is not None
    assert event.available_tools is not None
    assert event.used_tool_ids is not None

    servers = {s.name: s for s in event.available_tool_servers}
    assert servers["excalidraw"].kind == "mcp"
    assert servers["builtin"].kind == "runtime"

    tools_by_name = {t.name: t for t in event.available_tools}
    assert tools_by_name["create_view"].tool_server_id == servers["excalidraw"].id
    assert tools_by_name["Bash"].tool_server_id == servers["builtin"].id

    assert event.used_tool_ids == [tools_by_name["create_view"].id]
    assert event.stop_reason == "tool_use"


def test_build_event_populates_raw_model_id() -> None:
    event = build_event(_mil_record(), identity_source_type="manual_import")
    assert event is not None
    assert event.model.id == "us.anthropic.claude-sonnet-4-6"
    assert event.model.raw_model_id == "us.anthropic.claude-sonnet-4-6"


def test_unknown_stop_reason_falls_back_to_unknown() -> None:
    event = build_event(
        _mil_record(output={"outputTokenCount": 5, "outputBodyJson": {"stopReason": "wat"}}),
        identity_source_type="manual_import",
    )
    assert event is not None
    assert event.stop_reason == "unknown"


def test_invalid_stop_reason_literal_rejected_on_construction() -> None:
    """Pydantic Literal type rejects values outside the AIStopReason enum."""
    from pydantic import ValidationError

    from slashid_bedrock_forwarder.events import AIInvocationObservedV1, AIModel

    with pytest.raises(ValidationError):
        AIInvocationObservedV1(
            request_id="r",
            timestamp="t",
            identifier_from_source="x",
            identity_source_type="manual_import",
            model=AIModel(id="m"),
            stop_reason="not-a-real-reason",  # ty: ignore[invalid-argument-type]
        )


def test_build_event_wire_form_drops_none_optional_fields() -> None:
    event = build_event(_mil_record(), identity_source_type="manual_import")
    assert event is not None
    wire = event.model_dump(mode="json", exclude_none=True)
    # The server derives org_id + connection_id from the token; we drop them.
    assert "org_id" not in wire
    assert "connection_id" not in wire
    # No empty None placeholders for the rest either.
    assert "available_tool_servers" not in wire
    assert "available_tools" not in wire
    assert "used_tool_ids" not in wire
    # Tokens are always present (default 0s).
    assert wire["tokens"] == {
        "input": 100,
        "output": 50,
        "cache_read": 5,
        "cache_write": 0,
        "reasoning": 0,
    }
