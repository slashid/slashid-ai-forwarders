"""Tests for Anthropic-shape → Converse-shape rewriting."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from slashid_bedrock_forwarder.mil_normalize import normalize_record


def test_already_converse_shape_passes_through() -> None:
    record: dict[str, Any] = {
        "input": {"inputBodyJson": {"toolConfig": {"tools": []}}},
        "output": {"outputBodyJson": {"output": {"message": {"content": []}}}},
    }
    out = normalize_record(record)
    assert out["input"]["inputBodyJson"] == {"toolConfig": {"tools": []}}


def test_anthropic_tools_rewritten_to_toolconfig() -> None:
    record = {
        "input": {
            "inputBodyJson": {
                "tools": [
                    {
                        "name": "search",
                        "description": "search the web",
                        "input_schema": {"type": "object", "properties": {"q": {}}},
                    }
                ],
            }
        },
        "output": {"outputBodyJson": {}},
    }
    out = normalize_record(record)
    tools = out["input"]["inputBodyJson"]["toolConfig"]["tools"]
    assert tools == [
        {
            "toolSpec": {
                "name": "search",
                "description": "search the web",
                "inputSchema": {"json": {"type": "object", "properties": {"q": {}}}},
            }
        }
    ]


def test_anthropic_stream_text_block_reconstructed() -> None:
    record = {
        "input": {"inputBodyJson": {}},
        "output": {
            "outputBodyJson": [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hello"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": " world"},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            ]
        },
    }
    out = normalize_record(record)
    body = out["output"]["outputBodyJson"]
    assert body["stopReason"] == "end_turn"
    assert body["output"]["message"]["content"] == [{"text": "hello world"}]


def test_anthropic_stream_tool_use_block_reconstructed() -> None:
    record = {
        "input": {"inputBodyJson": {}},
        "output": {
            "outputBodyJson": [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu-1", "name": "Bash"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"ls"}'},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            ]
        },
    }
    out = normalize_record(record)
    body = out["output"]["outputBodyJson"]
    assert body["stopReason"] == "tool_use"
    block = body["output"]["message"]["content"][0]["toolUse"]
    assert block["toolUseId"] == "toolu-1"
    assert block["name"] == "Bash"
    assert block["input"] == {"cmd": "ls"}


def test_malformed_tool_input_json_does_not_leak_to_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Regression for B3: malformed tool_use input_json log must not contain
    the raw buffer bytes — those can hold tool arguments and the customer's
    own log group would see them."""
    record = {
        "input": {"inputBodyJson": {}},
        "output": {
            "outputBodyJson": [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu-1", "name": "Bash"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    # truncated → invalid JSON; payload would otherwise be visible.
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"command": "rm -rf /secret/path",',
                    },
                },
                {"type": "content_block_stop", "index": 0},
            ]
        },
    }
    caplog.set_level(logging.WARNING)
    normalize_record(record)

    relevant = [r for r in caplog.records if "tool_use input_json malformed" in r.getMessage()]
    assert relevant, "expected the malformed-input warning to fire"
    for r in relevant:
        msg = r.getMessage()
        # The sensitive content must not appear.
        assert "rm -rf" not in msg
        assert "secret" not in msg
        assert "command" not in msg
