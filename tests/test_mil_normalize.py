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
    """Tools normalization runs when the output shape identifies the record
    as Anthropic — either the non-streaming dict here, or a streaming
    events list (covered by the stream tests below)."""
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
        "output": {
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            }
        },
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


def test_non_anthropic_stream_list_left_alone() -> None:
    """A list of stream events that doesn't use Anthropic's event vocabulary
    is left in place. Non-Anthropic Bedrock streams (Nova, Titan, Cohere)
    use different `type` values; we don't own their normalization yet."""
    nova_stream = [
        {"type": "chunk", "output": {"partial": "hello"}},
        {"type": "chunk", "output": {"partial": " world"}},
        {"type": "chunk", "output": {"stopReason": "COMPLETE"}},
    ]
    record = {
        "input": {"inputBodyJson": {"tools": [{"name": "x"}]}},
        "output": {"outputBodyJson": list(nova_stream)},
    }
    out = normalize_record(record)
    # Output is untouched — we don't claim ownership of this shape.
    assert out["output"]["outputBodyJson"] == nova_stream
    # Input tools are also untouched (dispatch is family-scoped: no Anthropic
    # output → no Anthropic input tools rewrite).
    assert "toolConfig" not in out["input"]["inputBodyJson"]


def test_anthropic_message_backfills_missing_top_level_tokens() -> None:
    """Non-streaming Anthropic MIL records omit cache-token counts at the top
    level; the numbers are only in `body.usage`. Verified by live inspection
    of an InvokeModel call against `us.anthropic.claude-sonnet-4-6` on
    2026-07-01: `input.cacheReadInputTokenCount` and
    `cacheWriteInputTokenCount` were null while `body.usage` had them.

    The normalizer must copy the missing fields over from `body.usage`
    before discarding the body, so downstream extraction reads a uniform
    top-level shape.
    """
    record: dict[str, Any] = {
        "input": {
            "inputBodyJson": {},
            # MIL fills inputTokenCount but not the cache fields for InvokeModel
            "inputTokenCount": 600,
        },
        "output": {
            "outputTokenCount": 76,
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 600,
                    "output_tokens": 76,
                    "cache_read_input_tokens": 42,
                    "cache_creation_input_tokens": 17,
                },
            },
        },
    }
    normalize_record(record)
    # Original MIL-populated fields untouched
    assert record["input"]["inputTokenCount"] == 600
    assert record["output"]["outputTokenCount"] == 76
    # Cache fields backfilled from body.usage
    assert record["input"]["cacheReadInputTokenCount"] == 42
    assert record["input"]["cacheWriteInputTokenCount"] == 17


def test_anthropic_message_backfill_never_overrides_existing_mil_value() -> None:
    """When MIL provides a top-level token count, it wins over `body.usage`."""
    record: dict[str, Any] = {
        "input": {
            "inputBodyJson": {},
            "inputTokenCount": 111,
            "cacheReadInputTokenCount": 222,
        },
        "output": {
            "outputTokenCount": 333,
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                # These would rewrite everything if backfill wasn't idempotent
                "usage": {
                    "input_tokens": 999,
                    "output_tokens": 999,
                    "cache_read_input_tokens": 999,
                    "cache_creation_input_tokens": 999,
                },
            },
        },
    }
    normalize_record(record)
    assert record["input"]["inputTokenCount"] == 111
    assert record["input"]["cacheReadInputTokenCount"] == 222
    assert record["output"]["outputTokenCount"] == 333
    # Only the absent field gets backfilled
    assert record["input"]["cacheWriteInputTokenCount"] == 999


def test_anthropic_stream_backfills_tokens_from_usage_events() -> None:
    """Streaming carries usage in `message_start` and `message_delta`; if
    MIL top-level counts happen to be missing on some record, we recover
    from the events. Defense-in-depth (streaming top-level fields are
    normally populated) but covers the same conceptual gap symmetrically."""
    record: dict[str, Any] = {
        "input": {"inputBodyJson": {}},
        "output": {
            "outputBodyJson": [
                {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 50,
                            "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": 25,
                        }
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hi"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 3},
                },
            ]
        },
    }
    normalize_record(record)
    assert record["input"]["inputTokenCount"] == 50
    assert record["input"]["cacheReadInputTokenCount"] == 100
    assert record["input"]["cacheWriteInputTokenCount"] == 25
    assert record["output"]["outputTokenCount"] == 3


def test_anthropic_stream_input_tools_rewritten_with_output() -> None:
    """Input tools are normalized as part of the Anthropic-stream dispatch,
    not as an independent step. Verifies the family-scoped ownership."""
    record = {
        "input": {
            "inputBodyJson": {
                "tools": [
                    {"name": "search", "description": "d", "input_schema": {"type": "object"}}
                ],
            }
        },
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
                    "delta": {"type": "text_delta", "text": "ok"},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            ]
        },
    }
    out = normalize_record(record)
    # Input tools rewritten because output identified as Anthropic stream.
    assert "toolConfig" in out["input"]["inputBodyJson"]
    # Output reconstructed from stream events.
    assert out["output"]["outputBodyJson"]["stopReason"] == "end_turn"
