"""Pure-function transforms: Anthropic -> Converse."""

from __future__ import annotations

import logging

import pytest
from pydantic import TypeAdapter

from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    extract_stream_usage,
    message_to_converse,
    stream_to_converse,
    tools_to_converse_tool_config,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicMessage,
    AnthropicStreamEvent,
    AnthropicToolDeclaration,
)

_STREAM = TypeAdapter(list[AnthropicStreamEvent])


# --------------------------------------------------------------------------
# message_to_converse
# --------------------------------------------------------------------------


def test_anthropic_message_text_and_tool_use_to_converse() -> None:
    msg = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me check"},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "read",
                    "input": {"path": "/x"},
                },
            ],
            "stop_reason": "tool_use",
        }
    )
    result = message_to_converse(msg)
    assert result.model_dump(exclude_none=True) == {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "let me check"},
                    {
                        "toolUse": {
                            "toolUseId": "toolu_abc",
                            "name": "read",
                            "input": {"path": "/x"},
                        }
                    },
                ],
            },
        },
        "stopReason": "tool_use",
    }


def test_anthropic_message_thinking_folded_to_text() -> None:
    msg = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reasoning...", "signature": "sig"},
                {"type": "text", "text": "answer"},
            ],
            "stop_reason": "end_turn",
        }
    )
    result = message_to_converse(msg)
    assert result.model_dump(exclude_none=True) == {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "reasoning..."},
                    {"text": "answer"},
                ],
            },
        },
        "stopReason": "end_turn",
    }


def test_anthropic_message_empty_content() -> None:
    msg = AnthropicMessage.model_validate({"type": "message", "role": "assistant", "content": []})
    result = message_to_converse(msg)
    assert result.model_dump(exclude_none=True) == {
        "output": {"message": {"role": "assistant", "content": []}},
    }


def test_anthropic_message_unknown_block_skipped_silently() -> None:
    # Unknown content block types don't propagate to the Converse output.
    # Documented behaviour in 1.1; may become warn-once in a follow-up.
    msg = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "before"},
                {"type": "server_tool_use", "id": "srv_1", "name": "web_search"},
                {"type": "text", "text": "after"},
            ],
            "stop_reason": "end_turn",
        }
    )
    result = message_to_converse(msg)
    assert result.model_dump(exclude_none=True) == {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "before"},
                    {"text": "after"},
                ],
            },
        },
        "stopReason": "end_turn",
    }


def test_anthropic_message_no_stop_reason_omits_key() -> None:
    msg = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
        }
    )
    result = message_to_converse(msg)
    dumped = result.model_dump(exclude_none=True)
    assert "stopReason" not in dumped


def test_anthropic_message_tool_use_with_null_input_becomes_empty_dict() -> None:
    # Byte-parity with legacy: falsy input (None or absent) becomes {}.
    msg = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_x", "name": "noop"},
            ],
            "stop_reason": "tool_use",
        }
    )
    result = message_to_converse(msg)
    assert result.model_dump(exclude_none=True) == {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "toolu_x", "name": "noop", "input": {}}},
                ],
            },
        },
        "stopReason": "tool_use",
    }


# --------------------------------------------------------------------------
# stream_to_converse
# --------------------------------------------------------------------------


def test_anthropic_stream_text_only() -> None:
    events = _STREAM.validate_python(
        [
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
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        ]
    )
    result = stream_to_converse(events)
    assert result.model_dump(exclude_none=True) == {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
    }


def test_anthropic_stream_tool_use_with_input_json_deltas() -> None:
    events = _STREAM.validate_python(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"/x"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        ]
    )
    result = stream_to_converse(events)
    assert result.model_dump(exclude_none=True) == {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "toolu_1",
                            "name": "read",
                            "input": {"path": "/x"},
                        }
                    },
                ],
            },
        },
        "stopReason": "tool_use",
    }


def test_anthropic_stream_malformed_input_json_yields_empty_dict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A partial_json that never assembles into valid JSON should log a
    # warning with byte count and produce {} — matches Phase 1 behaviour.
    events = _STREAM.validate_python(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "not json"},
            },
            {"type": "content_block_stop", "index": 0},
        ]
    )
    with caplog.at_level(
        logging.WARNING,
        logger="slashid_ai_forwarder_core.normalize.anthropic.normalize",
    ):
        result = stream_to_converse(events)
    block = result.output.message.content[0]
    # Grab the toolUse block's input via model_dump for symmetry.
    dumped = block.model_dump(exclude_none=True)
    assert dumped == {"toolUse": {"toolUseId": "toolu_1", "name": "read", "input": {}}}
    assert any("tool_use input_json malformed" in r.message for r in caplog.records)
    # The byte count is present in the fully-formatted log message.
    assert any("8 bytes" in r.getMessage() for r in caplog.records)


def test_anthropic_stream_thinking_folded_to_text() -> None:
    events = _STREAM.validate_python(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "reasoning..."},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        ]
    )
    result = stream_to_converse(events)
    assert result.model_dump(exclude_none=True) == {
        "output": {"message": {"role": "assistant", "content": [{"text": "reasoning..."}]}},
        "stopReason": "end_turn",
    }


def test_anthropic_stream_empty_event_list() -> None:
    events = _STREAM.validate_python([])
    result = stream_to_converse(events)
    assert result.model_dump(exclude_none=True) == {
        "output": {"message": {"role": "assistant", "content": []}},
    }


def test_anthropic_stream_no_stop_reason_omits_stopreason_key() -> None:
    # No message_delta with a stop_reason -> stopReason key is absent
    # from the dumped output.
    events = _STREAM.validate_python(
        [
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
        ]
    )
    result = stream_to_converse(events)
    dumped = result.model_dump(exclude_none=True)
    assert "stopReason" not in dumped
    assert dumped == {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
    }


def test_anthropic_stream_unknown_content_block_no_slot() -> None:
    # AnthropicUnknownBlock in content_block_start allocates no slot;
    # subsequent deltas and the stop event are safe no-ops.
    events = _STREAM.validate_python(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "server_tool_use", "id": "srv"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ignored"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        ]
    )
    result = stream_to_converse(events)
    assert result.model_dump(exclude_none=True) == {
        "output": {"message": {"role": "assistant", "content": []}},
        "stopReason": "end_turn",
    }


def test_anthropic_stream_ping_and_message_stop_are_noops() -> None:
    events = _STREAM.validate_python(
        [
            {"type": "ping"},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "ping"},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hi"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]
    )
    result = stream_to_converse(events)
    assert result.model_dump(exclude_none=True) == {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
    }


# --------------------------------------------------------------------------
# tools_to_converse_tool_config
# --------------------------------------------------------------------------


def test_anthropic_tools_to_converse_tool_config() -> None:
    tools = [
        AnthropicToolDeclaration.model_validate(
            {
                "name": "read",
                "description": "Read a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ),
    ]
    result = tools_to_converse_tool_config(tools)
    assert result.model_dump(by_alias=True, exclude_none=True) == {
        "tools": [
            {
                "toolSpec": {
                    "name": "read",
                    "description": "Read a file.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        }
                    },
                },
            },
        ],
    }


def test_anthropic_tools_to_converse_tool_config_empty_input() -> None:
    result = tools_to_converse_tool_config([])
    assert result.model_dump(by_alias=True, exclude_none=True) == {"tools": []}


def test_anthropic_tools_to_converse_tool_config_missing_input_schema() -> None:
    tools = [AnthropicToolDeclaration.model_validate({"name": "noop"})]
    result = tools_to_converse_tool_config(tools)
    assert result.tools[0].toolSpec.inputSchema.json_ == {}
    # Wire form: {"json": {}}
    assert result.model_dump(by_alias=True, exclude_none=True) == {
        "tools": [
            {
                "toolSpec": {
                    "name": "noop",
                    "inputSchema": {"json": {}},
                },
            },
        ],
    }


# --------------------------------------------------------------------------
# extract_stream_usage
# --------------------------------------------------------------------------


def test_extract_stream_usage_merges_start_and_delta() -> None:
    events = _STREAM.validate_python(
        [
            {
                "type": "message_start",
                "message": {
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 3},
                },
            },
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 12}},
        ]
    )
    usage = extract_stream_usage(events)
    assert usage.input_tokens == 10
    assert usage.output_tokens == 12
    assert usage.cache_read_input_tokens == 3


def test_extract_stream_usage_empty_events() -> None:
    usage = extract_stream_usage(_STREAM.validate_python([]))
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cache_read_input_tokens is None
    assert usage.cache_creation_input_tokens is None
    assert usage.model_dump(exclude_none=True) == {}


def test_extract_stream_usage_delta_only() -> None:
    # Some responses only carry usage in message_delta.
    events = _STREAM.validate_python(
        [
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 5}},
        ]
    )
    usage = extract_stream_usage(events)
    assert usage.output_tokens == 5
    assert usage.input_tokens is None
