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


def test_anthropic_nonstreaming_response_rewritten() -> None:
    """Non-streaming InvokeModel-against-Anthropic response reaches Converse shape.

    Regression: before the fix, `_used_tool_ids` (which reads from
    `output.outputBodyJson.output.message.content[].toolUse`) returned empty
    for these records because the body sat at `outputBodyJson.content[]`
    with an Anthropic-native shape. Every non-streaming Anthropic
    invocation dropped its tool-use signal silently.
    """
    record = {
        "input": {"inputBodyJson": {}},
        "output": {
            "outputBodyJson": {
                "id": "msg_01ABC",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check that."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01XYZ",
                        "name": "Read",
                        "input": {"file_path": "/tmp/x"},
                    },
                ],
                "stop_reason": "tool_use",
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 30},
            }
        },
    }
    out = normalize_record(record)
    body = out["output"]["outputBodyJson"]

    assert body["stopReason"] == "tool_use"
    content = body["output"]["message"]["content"]
    assert body["output"]["message"]["role"] == "assistant"
    assert content == [
        {"text": "Let me check that."},
        {
            "toolUse": {
                "toolUseId": "toolu_01XYZ",
                "name": "Read",
                "input": {"file_path": "/tmp/x"},
            }
        },
    ]


def test_anthropic_nonstreaming_missing_type_field_not_touched() -> None:
    """A dict without the full `type` + `role` + `content` triad is left alone.

    Detection requires all three markers together — otherwise we risk
    misclassifying unrelated dict shapes as Anthropic responses.
    """
    ambiguous = {
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
    }
    record = {
        "input": {"inputBodyJson": {}},
        "output": {"outputBodyJson": dict(ambiguous)},
    }
    out = normalize_record(record)
    assert out["output"]["outputBodyJson"] == ambiguous


def test_anthropic_nonstreaming_thinking_block_folded_into_text() -> None:
    """`thinking` content blocks become plain text — consistent with the
    streaming reconstruction path, since downstream code only cares about
    text vs. tool_use."""
    record = {
        "input": {"inputBodyJson": {}},
        "output": {
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "reasoning..."},
                    {"type": "text", "text": "answer"},
                ],
                "stop_reason": "end_turn",
            }
        },
    }
    out = normalize_record(record)
    content = out["output"]["outputBodyJson"]["output"]["message"]["content"]
    assert content == [{"text": "reasoning..."}, {"text": "answer"}]


def test_converse_response_not_reprocessed() -> None:
    """A Converse-shape response passes through untouched even though it's a dict."""
    original = {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
    }
    record = {
        "input": {"inputBodyJson": {}},
        "output": {"outputBodyJson": dict(original)},
    }
    out = normalize_record(record)
    assert out["output"]["outputBodyJson"] == original
