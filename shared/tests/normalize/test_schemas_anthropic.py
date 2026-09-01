"""Anthropic wire-schema round-trips and discriminator behaviour."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicContentBlockDeltaEvent,
    AnthropicContentBlockStart,
    AnthropicContentBlockStop,
    AnthropicInputJsonDelta,
    AnthropicMessage,
    AnthropicMessageDelta,
    AnthropicMessageStart,
    AnthropicMessageStop,
    AnthropicPing,
    AnthropicStreamEvent,
    AnthropicTextBlock,
    AnthropicTextDelta,
    AnthropicThinkingBlock,
    AnthropicThinkingDelta,
    AnthropicToolUseBlock,
    AnthropicUnknownBlock,
    AnthropicUsage,
)

# --------------------------------------------------------------------------
# Response message
# --------------------------------------------------------------------------


def test_anthropic_message_round_trip_text_only() -> None:
    raw = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
    }
    msg = AnthropicMessage.model_validate(raw)
    assert isinstance(msg.content[0], AnthropicTextBlock)
    assert msg.content[0].text == "hello"
    assert msg.stop_reason == "end_turn"
    assert msg.model_dump(exclude_none=True) == raw


def test_anthropic_message_round_trip_tool_use_and_thinking() -> None:
    raw = {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "id": "toolu_abc", "name": "read", "input": {"path": "/x"}},
            {"type": "thinking", "thinking": "reasoning", "signature": "sig"},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 10, "cache_read_input_tokens": 2},
    }
    msg = AnthropicMessage.model_validate(raw)
    assert isinstance(msg.content[0], AnthropicTextBlock)
    assert isinstance(msg.content[1], AnthropicToolUseBlock)
    assert isinstance(msg.content[2], AnthropicThinkingBlock)
    assert msg.content[1].id == "toolu_abc"
    assert msg.usage is not None
    assert msg.usage.input_tokens == 5
    assert msg.model_dump(exclude_none=True) == raw


def test_anthropic_message_unknown_content_block_falls_through_to_catchall() -> None:
    raw = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "server_tool_use", "id": "srv_1", "name": "web_search"}],
    }
    msg = AnthropicMessage.model_validate(raw)
    assert isinstance(msg.content[0], AnthropicUnknownBlock)
    assert msg.content[0].type == "server_tool_use"


def test_anthropic_message_extra_field_ignored() -> None:
    # Anthropic may add a new top-level field we don't model; must not fail.
    raw = {
        "type": "message",
        "role": "assistant",
        "content": [],
        "some_future_field": {"foo": "bar"},
    }
    msg = AnthropicMessage.model_validate(raw)
    # Field ignored, not preserved on dump.
    assert "some_future_field" not in msg.model_dump(exclude_none=True)


def test_anthropic_message_wrong_role_rejected() -> None:
    raw = {"type": "message", "role": "user", "content": []}
    with pytest.raises(ValidationError):
        AnthropicMessage.model_validate(raw)


def test_anthropic_message_empty_content_allowed() -> None:
    raw = {"type": "message", "role": "assistant", "content": []}
    msg = AnthropicMessage.model_validate(raw)
    assert msg.content == []
    assert msg.model_dump(exclude_none=True) == raw


def test_anthropic_usage_all_optional() -> None:
    # All fields optional — an empty usage dict validates.
    usage = AnthropicUsage.model_validate({})
    assert usage.input_tokens is None
    assert usage.model_dump(exclude_none=True) == {}


# --------------------------------------------------------------------------
# Stream events
# --------------------------------------------------------------------------


_STREAM_ADAPTER = TypeAdapter(list[AnthropicStreamEvent])


def test_stream_full_message_lifecycle() -> None:
    raw = [
        {
            "type": "message_start",
            "message": {
                "type": "message",
                "role": "assistant",
                "content": [],
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 3},
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
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    events = _STREAM_ADAPTER.validate_python(raw)
    assert isinstance(events[0], AnthropicMessageStart)
    assert events[0].message is not None
    assert events[0].message.usage is not None
    assert events[0].message.usage.input_tokens == 10
    assert isinstance(events[1], AnthropicContentBlockStart)
    assert events[1].index == 0
    assert isinstance(events[2], AnthropicContentBlockDeltaEvent)
    assert isinstance(events[2].delta, AnthropicTextDelta)
    assert events[2].delta.text == "hi"
    assert isinstance(events[3], AnthropicContentBlockStop)
    assert isinstance(events[4], AnthropicMessageDelta)
    assert events[4].usage is not None
    assert events[4].usage.output_tokens == 5
    assert isinstance(events[5], AnthropicMessageStop)


def test_stream_tool_use_input_json_delta() -> None:
    raw = [
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
    ]
    events = _STREAM_ADAPTER.validate_python(raw)
    assert isinstance(events[0], AnthropicContentBlockStart)
    assert events[0].content_block.type == "tool_use"
    assert isinstance(events[0].content_block, AnthropicToolUseBlock)
    assert events[0].content_block.id == "toolu_1"
    assert isinstance(events[1], AnthropicContentBlockDeltaEvent)
    assert isinstance(events[1].delta, AnthropicInputJsonDelta)
    assert events[1].delta.partial_json == '{"path":'


def test_stream_thinking_delta() -> None:
    raw = [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reasoning..."},
        },
    ]
    events = _STREAM_ADAPTER.validate_python(raw)
    assert isinstance(events[0], AnthropicContentBlockDeltaEvent)
    assert isinstance(events[0].delta, AnthropicThinkingDelta)
    assert events[0].delta.thinking == "reasoning..."


def test_stream_unknown_event_type_fails_validation() -> None:
    # Load-bearing for stream detection: non-Anthropic Bedrock streams
    # (Nova/Titan/Cohere) must fail validation here so the dispatcher
    # falls through to parsed_as="unknown" instead of wrongly claiming
    # ownership. Anthropic adds new event types rarely — see the note
    # in schema.py above AnthropicStreamEvent.
    raw = [{"type": "some_future_event", "payload": {"foo": 1}}]
    with pytest.raises(ValidationError):
        _STREAM_ADAPTER.validate_python(raw)


def test_stream_mixed_known_and_unknown_events_fail_validation() -> None:
    # One unknown event mixed with known ones invalidates the whole list.
    # If Anthropic adds a new event type in a real stream, we get a
    # WARNING via the dispatcher fallthrough and add the class here.
    raw = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "some_future_event", "payload": {}},
        {"type": "message_stop"},
    ]
    with pytest.raises(ValidationError):
        _STREAM_ADAPTER.validate_python(raw)


def test_stream_ping() -> None:
    raw = [{"type": "ping"}]
    events = _STREAM_ADAPTER.validate_python(raw)
    assert isinstance(events[0], AnthropicPing)


def test_stream_non_list_rejected() -> None:
    with pytest.raises(ValidationError):
        _STREAM_ADAPTER.validate_python({"type": "message_start"})
