"""Tests for Anthropic-shape → Converse-shape pure functions.

These functions are envelope-agnostic: they receive/return payload-shaped
data and never touch a Bedrock MIL or Vertex log envelope. The equivalent
MIL-envelope-aware behaviour is exercised in bedrock/tests/test_mil_normalize.py.
"""

from __future__ import annotations

import logging

import pytest

from slashid_ai_forwarder_core.normalize.anthropic import (
    anthropic_message_to_converse,
    anthropic_stream_to_converse,
    anthropic_tools_to_converse_tool_config,
    extract_anthropic_stream_usage,
    looks_like_anthropic_message,
    looks_like_anthropic_stream,
)

# ---------- anthropic_message_to_converse ----------


def test_anthropic_message_text_and_tool_use_to_converse() -> None:
    body = {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "let me check"},
            {
                "type": "tool_use",
                "id": "toolu_abc",
                "name": "read_file",
                "input": {"path": "/x"},
            },
        ],
        "stop_reason": "tool_use",
    }
    assert anthropic_message_to_converse(body) == {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "let me check"},
                    {
                        "toolUse": {
                            "toolUseId": "toolu_abc",
                            "name": "read_file",
                            "input": {"path": "/x"},
                        }
                    },
                ],
            }
        },
        "stopReason": "tool_use",
    }


def test_anthropic_message_thinking_block_folded_into_text() -> None:
    """A `thinking` content block becomes a plain text block. Downstream code
    doesn't need a separate reasoning concept."""
    body = {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "reasoning..."},
            {"type": "text", "text": "answer"},
        ],
        "stop_reason": "end_turn",
    }
    result = anthropic_message_to_converse(body)
    assert result["output"]["message"]["content"] == [
        {"text": "reasoning..."},
        {"text": "answer"},
    ]
    assert result["stopReason"] == "end_turn"


def test_anthropic_message_missing_tool_input_defaults_to_empty_dict() -> None:
    body = {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "X"},
        ],
    }
    result = anthropic_message_to_converse(body)
    assert result["output"]["message"]["content"][0]["toolUse"]["input"] == {}
    # No stop_reason → no stopReason field
    assert "stopReason" not in result


def test_anthropic_message_ignores_non_dict_content_blocks() -> None:
    body = {
        "type": "message",
        "role": "assistant",
        "content": ["not-a-dict", {"type": "text", "text": "ok"}],
    }
    assert anthropic_message_to_converse(body)["output"]["message"]["content"] == [
        {"text": "ok"}
    ]


# ---------- anthropic_stream_to_converse ----------


def test_anthropic_stream_text_and_stop_reason() -> None:
    events = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 3}},
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": " world"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        },
    ]
    result = anthropic_stream_to_converse(events)
    assert result == {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi world"}]}},
        "stopReason": "end_turn",
    }


def test_anthropic_stream_tool_use_with_input_json_deltas() -> None:
    events = [
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
    result = anthropic_stream_to_converse(events)
    block = result["output"]["message"]["content"][0]["toolUse"]
    assert block == {"toolUseId": "toolu-1", "name": "Bash", "input": {"cmd": "ls"}}
    assert result["stopReason"] == "tool_use"


def test_anthropic_stream_thinking_block_folded_into_text() -> None:
    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "let me think..."},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    result = anthropic_stream_to_converse(events)
    assert result["output"]["message"]["content"] == [{"text": "let me think..."}]


def test_anthropic_stream_malformed_tool_input_json_logs_length_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard: malformed input_json warning must not leak buffer bytes."""
    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu-1", "name": "Bash"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"command": "rm -rf /secret/path",',
            },
        },
        {"type": "content_block_stop", "index": 0},
    ]
    caplog.set_level(logging.WARNING)
    result = anthropic_stream_to_converse(events)
    # Tool use appended with empty input on malformed JSON.
    assert result["output"]["message"]["content"][0]["toolUse"]["input"] == {}
    relevant = [r for r in caplog.records if "tool_use input_json malformed" in r.getMessage()]
    assert relevant, "expected the malformed-input warning to fire"
    for r in relevant:
        msg = r.getMessage()
        assert "rm -rf" not in msg
        assert "secret" not in msg


def test_anthropic_stream_no_stop_reason_omits_stopreason_key() -> None:
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
        {"type": "content_block_stop", "index": 0},
    ]
    result = anthropic_stream_to_converse(events)
    assert "stopReason" not in result
    assert result["output"]["message"]["content"] == [{"text": "x"}]


# ---------- anthropic_tools_to_converse_tool_config ----------


def test_anthropic_tools_flat_to_converse_toolconfig() -> None:
    tools = [
        {
            "name": "read_file",
            "description": "Read a file.",
            "input_schema": {"type": "object"},
        },
    ]
    assert anthropic_tools_to_converse_tool_config(tools) == {
        "tools": [
            {
                "toolSpec": {
                    "name": "read_file",
                    "description": "Read a file.",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
    }


def test_anthropic_tools_empty_list_returns_empty_dict() -> None:
    assert anthropic_tools_to_converse_tool_config([]) == {}


def test_anthropic_tools_non_list_returns_empty_dict() -> None:
    assert anthropic_tools_to_converse_tool_config(None) == {}  # type: ignore[arg-type]
    assert anthropic_tools_to_converse_tool_config("not-a-list") == {}  # type: ignore[arg-type]


def test_anthropic_tools_missing_input_schema_becomes_empty_json_schema() -> None:
    tools = [{"name": "x", "description": "d"}]
    result = anthropic_tools_to_converse_tool_config(tools)
    assert result["tools"][0]["toolSpec"]["inputSchema"] == {"json": {}}


# ---------- extract_anthropic_stream_usage ----------


def test_extract_anthropic_stream_usage_merges_start_and_delta() -> None:
    events = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 3}},
        },
        {"type": "message_delta", "usage": {"output_tokens": 12}},
    ]
    assert extract_anthropic_stream_usage(events) == {
        "input_tokens": 10,
        "cache_read_input_tokens": 3,
        "output_tokens": 12,
    }


def test_extract_anthropic_stream_usage_empty_when_no_usage_events() -> None:
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_stop", "index": 0},
    ]
    assert extract_anthropic_stream_usage(events) == {}


def test_extract_anthropic_stream_usage_ignores_non_dict_events() -> None:
    events = ["nonsense", None, {"type": "message_delta", "usage": {"output_tokens": 1}}]
    assert extract_anthropic_stream_usage(events) == {"output_tokens": 1}


# ---------- looks_like_anthropic_message ----------


def test_looks_like_anthropic_message_positive() -> None:
    assert looks_like_anthropic_message(
        {"type": "message", "role": "assistant", "content": []}
    )


def test_looks_like_anthropic_message_negative_converse_shape() -> None:
    # Converse shape has an `output` key at the top level.
    assert not looks_like_anthropic_message({"output": {"message": {}}})


def test_looks_like_anthropic_message_negative_missing_triad_marker() -> None:
    # Missing `type` field — the full type+role+content triad is required.
    assert not looks_like_anthropic_message(
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
    )


def test_looks_like_anthropic_message_negative_non_dict() -> None:
    assert not looks_like_anthropic_message([])
    assert not looks_like_anthropic_message("string")
    assert not looks_like_anthropic_message(None)


# ---------- looks_like_anthropic_stream ----------


def test_looks_like_anthropic_stream_positive() -> None:
    assert looks_like_anthropic_stream([{"type": "message_start"}])
    assert looks_like_anthropic_stream(
        [{"type": "content_block_delta", "index": 0, "delta": {}}]
    )


def test_looks_like_anthropic_stream_negative_non_list() -> None:
    assert not looks_like_anthropic_stream({"type": "message_start"})
    assert not looks_like_anthropic_stream(None)


def test_looks_like_anthropic_stream_negative_non_anthropic_vocabulary() -> None:
    # Nova / Titan / Cohere streams use different event vocabularies.
    assert not looks_like_anthropic_stream([{"type": "nova_delta"}])
    assert not looks_like_anthropic_stream([{"type": "chunk", "output": {"partial": "x"}}])


def test_looks_like_anthropic_stream_ignores_non_dict_entries() -> None:
    assert not looks_like_anthropic_stream(["nonsense", None, 42])
