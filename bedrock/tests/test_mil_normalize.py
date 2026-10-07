"""Tests for the MIL record dispatcher producing NormalizedInvocation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from slashid_ai_forwarder_core.config_base import BaseConfig

from slashid_bedrock_forwarder.mil_normalize import normalize_record

_CONFIG = BaseConfig(endpoint="http://test", push_token="test")
_FIXTURES = Path(__file__).parent / "fixtures"

# Minimal valid request bodies for each format — both request and response
# must validate, so every test needs an inputBodyJson that satisfies the
# request-side schema for the format under test.
_MIN_ANTHROPIC_REQUEST: dict[str, Any] = {
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
}
_MIN_CONVERSE_REQUEST: dict[str, Any] = {
    "messages": [{"role": "user", "content": [{"text": "hi"}]}],
}
_MIN_RESPONSES_REQUEST: dict[str, Any] = {"input": "hi"}


async def test_already_converse_shape_passes_through() -> None:
    """A Converse-shape record produces a NormalizedInvocation and leaves
    the raw record body untouched."""
    record: dict[str, Any] = {
        "input": {
            "inputBodyJson": {
                "toolConfig": {"tools": []},
                "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            }
        },
        "output": {
            "outputBodyJson": {
                "output": {"message": {"role": "assistant", "content": [{"text": "hello"}]}},
                "stopReason": "end_turn",
            }
        },
    }
    normalized = await normalize_record(record, config=_CONFIG)
    # Raw input body is preserved verbatim — no rewrite.
    assert record["input"]["inputBodyJson"] == {
        "toolConfig": {"tools": []},
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
    }
    assert record["_parsed_as"] == "bedrock-converse"
    assert normalized.output is not None
    assert normalized.output.stop_reason == "end_turn"


async def test_anthropic_tools_populate_tools_declared() -> None:
    """Anthropic ``request.tools[]`` land on ``normalized.input.tools_declared``
    as canonical ``AITool`` / ``AIToolServer`` entries — no more on-record
    Converse rewrite. Raw input body stays untouched."""
    record: dict[str, Any] = {
        "input": {
            "inputBodyJson": {
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
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
    normalized = await normalize_record(record, config=_CONFIG)
    # Raw input body preserved verbatim — no toolConfig injection.
    assert "toolConfig" not in record["input"]["inputBodyJson"]
    assert normalized.input.tools_declared is not None
    assert normalized.input.tool_servers is not None
    assert len(normalized.input.tools_declared) == 1
    tool = normalized.input.tools_declared[0]
    assert tool.name == "search"
    assert tool.description == "search the web"
    # "search" has no `mcp__` / `__` prefix → maps onto the synthetic
    # "builtin" server (runtime kind).
    server = normalized.input.tool_servers[0]
    assert server.name == "builtin"
    assert server.kind == "runtime"
    assert tool.tool_server_id == server.id


async def test_anthropic_stream_text_block_reconstructed() -> None:
    record = {
        "input": {"inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST)},
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
    normalized = await normalize_record(record, config=_CONFIG)
    assert normalized.output is not None
    assert normalized.output.stop_reason == "end_turn"
    assert normalized.output.message is not None
    text_parts = [c.text for c in normalized.output.message.content if c.kind == "text"]
    assert "".join(t or "" for t in text_parts) == "hello world"


async def test_anthropic_stream_tool_use_block_reconstructed() -> None:
    record = {
        "input": {"inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST)},
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
    normalized = await normalize_record(record, config=_CONFIG)
    assert normalized.output is not None
    assert normalized.output.stop_reason == "tool_use"
    assert normalized.output.message is not None
    tool_uses = [c for c in normalized.output.message.content if c.kind == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0].tool_use_id == "toolu-1"
    assert tool_uses[0].tool_name == "Bash"
    assert tool_uses[0].tool_input == {"cmd": "ls"}


async def test_malformed_tool_input_json_does_not_leak_to_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for B3: malformed tool_use input_json log must not contain
    the raw buffer bytes — those can hold tool arguments and the customer's
    own log group would see them."""
    record = {
        "input": {"inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST)},
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
    await normalize_record(record, config=_CONFIG)

    relevant = [r for r in caplog.records if "tool_use input_json malformed" in r.getMessage()]
    assert relevant, "expected the malformed-input warning to fire"
    for r in relevant:
        msg = r.getMessage()
        # The sensitive content must not appear.
        assert "rm -rf" not in msg
        assert "secret" not in msg


async def test_anthropic_nonstreaming_response_rewritten() -> None:
    """Non-streaming InvokeModel-against-Anthropic response reaches
    canonical NormalizedInvocation shape.

    Regression: before the fix, `used_tools_of` (which reads from
    `output.outputBodyJson.output.message.content[].toolUse`) returned empty
    for these records because the body sat at `outputBodyJson.content[]`
    with an Anthropic-native shape. Post-Chunk-8 we assert on the
    NormalizedInvocation directly — the record's raw body is untouched.
    """
    record = {
        "input": {"inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST)},
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
    normalized = await normalize_record(record, config=_CONFIG)

    assert normalized.output is not None
    assert normalized.output.stop_reason == "tool_use"
    assert normalized.output.message is not None
    assert normalized.output.message.role == "assistant"
    content = normalized.output.message.content
    assert len(content) == 2
    assert content[0].kind == "text"
    assert content[0].text == "Let me check that."
    assert content[1].kind == "tool_use"
    assert content[1].tool_use_id == "toolu_01XYZ"
    assert content[1].tool_name == "Read"
    assert content[1].tool_input == {"file_path": "/tmp/x"}


async def test_anthropic_nonstreaming_missing_type_field_not_touched() -> None:
    """A dict without the full `type` + `role` + `content` triad is not
    matched as an Anthropic message — it falls through to `unknown` and
    the raw body is preserved verbatim."""
    ambiguous = {
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
    }
    record = {
        "input": {"inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST)},
        "output": {"outputBodyJson": dict(ambiguous)},
    }
    await normalize_record(record, config=_CONFIG)
    # Raw body is preserved — we no longer mutate it.
    assert record["output"]["outputBodyJson"] == ambiguous
    # Neither request+response pair matches → unknown.
    assert record["_parsed_as"] == "unknown"


async def test_anthropic_nonstreaming_thinking_block_folded_into_text() -> None:
    """`thinking` content blocks become plain text — consistent with the
    streaming reconstruction path, since downstream code only cares about
    text vs. tool_use."""
    record = {
        "input": {"inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST)},
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
    normalized = await normalize_record(record, config=_CONFIG)
    assert normalized.output is not None
    assert normalized.output.message is not None
    content = normalized.output.message.content
    # thinking → reasoning kind; text → text kind.
    assert [(c.kind, c.text) for c in content] == [
        ("reasoning", "reasoning..."),
        ("text", "answer"),
    ]


async def test_converse_response_not_reprocessed() -> None:
    """A Converse-shape response passes through untouched — raw body preserved."""
    original = {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
    }
    record = {
        "input": {"inputBodyJson": dict(_MIN_CONVERSE_REQUEST)},
        "output": {"outputBodyJson": dict(original)},
    }
    await normalize_record(record, config=_CONFIG)
    # Raw output body preserved (no in-place rewrite).
    assert record["output"]["outputBodyJson"] == original
    assert record["_parsed_as"] == "bedrock-converse"


async def test_non_anthropic_stream_list_left_alone() -> None:
    """A list of stream events that doesn't use Anthropic's event vocabulary
    is left in place. Non-Anthropic Bedrock streams (Nova, Titan, Cohere)
    use different `type` values; we don't own their normalization yet."""
    nova_stream = [
        {"type": "chunk", "output": {"partial": "hello"}},
        {"type": "chunk", "output": {"partial": " world"}},
        {"type": "chunk", "output": {"stopReason": "COMPLETE"}},
    ]
    record = {
        "input": {
            "inputBodyJson": {
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                "tools": [{"name": "x"}],
            }
        },
        "output": {"outputBodyJson": list(nova_stream)},
    }
    await normalize_record(record, config=_CONFIG)
    # Output is untouched — we don't claim ownership of this shape.
    assert record["output"]["outputBodyJson"] == nova_stream
    # Input tools are also untouched (dispatch fell through — parsed_as="unknown").
    assert "toolConfig" not in record["input"]["inputBodyJson"]
    assert record["_parsed_as"] == "unknown"


async def test_anthropic_message_backfills_missing_top_level_tokens() -> None:
    """Non-streaming Anthropic MIL records omit cache-token counts at the top
    level; the numbers are only in `body.usage`. Verified by live inspection
    of an InvokeModel call against `us.anthropic.claude-sonnet-4-6` on
    2026-07-01: `input.cacheReadInputTokenCount` and
    `cacheWriteInputTokenCount` were null while `body.usage` had them.

    The normalizer must copy the missing fields over from `body.usage`
    so downstream extraction reads a uniform top-level shape.
    """
    record: dict[str, Any] = {
        "input": {
            "inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST),
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
    await normalize_record(record, config=_CONFIG)
    # Original MIL-populated fields untouched
    assert record["input"]["inputTokenCount"] == 600
    assert record["output"]["outputTokenCount"] == 76
    # Cache fields backfilled from body.usage
    assert record["input"]["cacheReadInputTokenCount"] == 42
    assert record["input"]["cacheWriteInputTokenCount"] == 17


async def test_anthropic_message_backfill_never_overrides_existing_mil_value() -> None:
    """When MIL provides a top-level token count, it wins over `body.usage`."""
    record: dict[str, Any] = {
        "input": {
            "inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST),
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
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 111
    assert record["input"]["cacheReadInputTokenCount"] == 222
    assert record["output"]["outputTokenCount"] == 333
    # Only the absent field gets backfilled
    assert record["input"]["cacheWriteInputTokenCount"] == 999


async def test_anthropic_stream_backfills_tokens_from_usage_events() -> None:
    """Streaming carries usage in `message_start` and `message_delta`; if
    MIL top-level counts happen to be missing on some record, we recover
    from the events. Defense-in-depth (streaming top-level fields are
    normally populated) but covers the same conceptual gap symmetrically."""
    record: dict[str, Any] = {
        "input": {"inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST)},
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
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 50
    assert record["input"]["cacheReadInputTokenCount"] == 100
    assert record["input"]["cacheWriteInputTokenCount"] == 25
    assert record["output"]["outputTokenCount"] == 3


async def test_anthropic_stream_input_tools_populate_tools_declared() -> None:
    """Anthropic-stream dispatch populates ``tools_declared`` from
    ``request.tools[]`` — the raw record body stays untouched."""
    record = {
        "input": {
            "inputBodyJson": {
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
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
    normalized = await normalize_record(record, config=_CONFIG)
    # Raw input body preserved verbatim.
    assert "toolConfig" not in record["input"]["inputBodyJson"]
    # Canonical carries the tool declaration + reconstructed stop reason.
    assert normalized.input.tools_declared is not None
    assert len(normalized.input.tools_declared) == 1
    assert normalized.input.tools_declared[0].name == "search"
    assert normalized.output is not None
    assert normalized.output.stop_reason == "end_turn"


# --------------------------------------------------------------------------
# Table-driven dispatch: parsed_as marker + fallthrough
# --------------------------------------------------------------------------


async def test_dispatch_anthropic_message_sets_parsed_as() -> None:
    record = {
        "input": {
            "inputBodyJson": {
                "anthropic_version": "bedrock-2023-05-31",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            }
        },
        "output": {
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
            }
        },
    }
    await normalize_record(record, config=_CONFIG)
    assert record["_parsed_as"] == "anthropic-message"


async def test_dispatch_anthropic_stream_sets_parsed_as() -> None:
    record = {
        "input": {
            "inputBodyJson": {
                "anthropic_version": "bedrock-2023-05-31",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
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
                    "delta": {"type": "text_delta", "text": "hi"},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            ]
        },
    }
    await normalize_record(record, config=_CONFIG)
    assert record["_parsed_as"] == "anthropic-stream"


async def test_dispatch_native_converse_sets_parsed_as() -> None:
    record = {
        "input": {"inputBodyJson": {"messages": [{"role": "user", "content": [{"text": "hi"}]}]}},
        "output": {
            "outputBodyJson": {
                "output": {"message": {"role": "assistant", "content": [{"text": "hello"}]}},
                "stopReason": "end_turn",
            }
        },
    }
    await normalize_record(record, config=_CONFIG)
    assert record["_parsed_as"] == "bedrock-converse"


async def test_dispatch_unknown_shape_marks_parsed_as_unknown_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record = {
        "modelId": "amazon.new-model-v1:0",
        "requestId": "req-xyz",
        "input": {
            "inputBodyJson": {
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
            }
        },
        "output": {"outputBodyJson": {"totally": "unknown", "shape": [1, 2, 3]}},
    }
    with caplog.at_level(logging.WARNING, logger="slashid_bedrock_forwarder.mil_normalize"):
        await normalize_record(record, config=_CONFIG)
    assert record["_parsed_as"] == "unknown"
    assert any(
        "unrecognized MIL body shape" in r.message and "amazon.new-model-v1:0" in r.message
        for r in caplog.records
    )


async def test_dispatch_backfill_tokens_from_anthropic_message_usage() -> None:
    # cache_creation_input_tokens should be copied to cacheWriteInputTokenCount.
    record = {
        "input": {"inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST)},
        "output": {
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [],
                "usage": {
                    "cache_creation_input_tokens": 42,
                    "input_tokens": 10,
                    "output_tokens": 5,
                },
            }
        },
    }
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["cacheWriteInputTokenCount"] == 42
    assert record["input"]["inputTokenCount"] == 10
    assert record["output"]["outputTokenCount"] == 5


async def test_dispatch_backfill_tokens_idempotent_does_not_overwrite() -> None:
    # If MIL already populated a field, backfill leaves it alone.
    record = {
        "input": {
            "inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST),
            "inputTokenCount": 999,
        },
        "output": {
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [],
                "usage": {"input_tokens": 10},
            }
        },
    }
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 999


# --------------------------------------------------------------------------
# Structural-exclusivity invariant: exactly one format's request+response
# adapters match each canonical record. Guards against a future schema loosening
# that would let two formats claim the same shape.
# --------------------------------------------------------------------------


from pydantic import ValidationError  # noqa: E402

from slashid_bedrock_forwarder.mil_normalize import _FORMATS  # noqa: E402


@pytest.mark.parametrize(
    "expected_name,request_body,payload",
    [
        (
            "anthropic-message",
            _MIN_ANTHROPIC_REQUEST,
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            },
        ),
        (
            "anthropic-stream",
            _MIN_ANTHROPIC_REQUEST,
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {"type": "content_block_stop", "index": 0},
            ],
        ),
        (
            "bedrock-converse",
            _MIN_CONVERSE_REQUEST,
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
            },
        ),
        (
            "openai-responses",
            _MIN_RESPONSES_REQUEST,
            {"object": "response", "id": "resp_1", "output": []},
        ),
        (
            "openai-responses-stream",
            _MIN_RESPONSES_REQUEST,
            [{"type": "response.created"}, {"type": "response.completed"}],
        ),
    ],
)
def test_format_structural_exclusivity(expected_name: str, request_body: Any, payload: Any) -> None:
    matches = []
    for fmt in _FORMATS:
        try:
            fmt.request_adapter.validate_python(request_body)
            fmt.response_adapter.validate_python(payload)
        except ValidationError:
            continue
        matches.append(fmt.name)
    assert matches == [expected_name], (
        f"Expected exactly {[expected_name]!r} to match, got {matches!r}. "
        "Two formats claiming the same record violates first-match-wins invariance."
    )


# --------------------------------------------------------------------------
# End-to-end: parsed_as flows into AIInvocationObservedV1
# --------------------------------------------------------------------------


async def test_e2e_unrecognized_shape_emits_parsed_as_unknown() -> None:
    """Full pipeline: unknown-format MIL record → normalize_record marks it
    with _parsed_as="unknown" → build_event_from_normalized surfaces it on
    the wire event. Identity, model, tokens survive; semantic fields
    (stop_reason, tools) are None/empty."""
    from slashid_ai_forwarder_core.events import build_event_from_normalized

    from slashid_bedrock_forwarder.event_envelope import bedrock_envelope

    record = {
        "schemaType": "ModelInvocationLog",
        "timestamp": "2026-09-01T12:00:00Z",
        "modelId": "amazon.hypothetical-model-v1:0",
        "requestId": "req-xyz",
        "region": "us-east-2",
        "identity": {"arn": "arn:aws:iam::123:user/x"},
        "input": {
            "inputBodyJson": {"unknown_request_shape": True},
            "inputTokenCount": 5,
        },
        "output": {
            "outputBodyJson": {"unknown_response_shape": True, "some_field": [1, 2]},
            "outputTokenCount": 3,
        },
    }
    normalized = await normalize_record(record, config=_CONFIG)
    envelope = bedrock_envelope(record)
    assert envelope is not None
    event = await build_event_from_normalized(normalized, envelope, config=_CONFIG)
    assert event.parsed_as == "unknown"
    # Semantic fields empty/best-effort on unknown-shape records.
    # No normalizer runs → NormalizedInvocation() default →
    # output.stop_reason defaults to "unknown" (the sentinel value),
    # which surfaces on the wire in place of a real vendor mapping.
    assert event.stop_reason == "unknown"
    assert event.used_tools is None
    assert event.available_tools is None
    # Model + identity + tokens survive from the envelope.
    assert event.model.raw_model_id == "amazon.hypothetical-model-v1:0"
    assert event.tokens.input == 5
    assert event.tokens.output == 3


async def test_e2e_anthropic_message_sets_parsed_as() -> None:
    """Happy path: Anthropic-message record → parsed_as="anthropic-message"."""
    from slashid_ai_forwarder_core.events import build_event_from_normalized

    from slashid_bedrock_forwarder.event_envelope import bedrock_envelope

    record = {
        "timestamp": "2026-09-01T12:00:00Z",
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "requestId": "req-ant",
        "region": "us-east-2",
        "identity": {"arn": "arn:aws:iam::123:user/x"},
        "input": {
            "inputBodyJson": dict(_MIN_ANTHROPIC_REQUEST),
            "inputTokenCount": 10,
        },
        "output": {
            "outputBodyJson": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
            },
            "outputTokenCount": 5,
        },
    }
    normalized = await normalize_record(record, config=_CONFIG)
    envelope = bedrock_envelope(record)
    assert envelope is not None
    event = await build_event_from_normalized(normalized, envelope, config=_CONFIG)
    assert event.parsed_as == "anthropic-message"


async def test_e2e_converse_response_sets_parsed_as() -> None:
    """Happy path: native Converse response → parsed_as="bedrock-converse"."""
    from slashid_ai_forwarder_core.events import build_event_from_normalized

    from slashid_bedrock_forwarder.event_envelope import bedrock_envelope

    record = {
        "timestamp": "2026-09-01T12:00:00Z",
        "modelId": "us.amazon.nova-pro-v1:0",
        "requestId": "req-nova",
        "region": "us-east-2",
        "identity": {"arn": "arn:aws:iam::123:user/x"},
        "input": {
            "inputBodyJson": dict(_MIN_CONVERSE_REQUEST),
            "inputTokenCount": 6,
        },
        "output": {
            "outputBodyJson": {
                "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
                "stopReason": "end_turn",
            },
            "outputTokenCount": 3,
        },
    }
    normalized = await normalize_record(record, config=_CONFIG)
    envelope = bedrock_envelope(record)
    assert envelope is not None
    event = await build_event_from_normalized(normalized, envelope, config=_CONFIG)
    assert event.parsed_as == "bedrock-converse"


# --------------------------------------------------------------------------
# OpenAI Responses
# --------------------------------------------------------------------------


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("openai_responses_mil.json", "openai-responses"),
        ("openai_responses_stream_mil.json", "openai-responses-stream"),
    ],
)
async def test_openai_responses_sets_parsed_as(fixture: str, expected: str) -> None:
    record = _fixture(fixture)
    normalized = await normalize_record(record, config=_CONFIG)
    assert record["_parsed_as"] == expected
    assert normalized.output.message is not None


@pytest.mark.parametrize(
    "fixture", ["openai_responses_mil.json", "openai_responses_stream_mil.json"]
)
def test_openai_responses_never_match_earlier_formats(fixture: str) -> None:
    record = _fixture(fixture)
    in_body = record["input"]["inputBodyJson"]
    out_body = record["output"]["outputBodyJson"]
    for fmt in _FORMATS:
        if fmt.name.startswith("openai-"):
            continue
        with pytest.raises(ValidationError):
            fmt.request_adapter.validate_python(in_body)
            fmt.response_adapter.validate_python(out_body)


async def test_openai_responses_overwrites_tokens_with_additive_split() -> None:
    record = _fixture("openai_responses_mil.json")
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 12
    assert record["input"]["cacheReadInputTokenCount"] == 0
    assert record["input"]["cacheWriteInputTokenCount"] == 0
    assert record["output"]["outputTokenCount"] == 11
    assert record["output"]["reasoningTokenCount"] == 12


async def test_openai_responses_stream_overwrites_tokens() -> None:
    record = _fixture("openai_responses_stream_mil.json")
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 56
    assert record["output"]["outputTokenCount"] == 19
    assert record["output"]["reasoningTokenCount"] == 0


async def test_openai_responses_cached_tokens_split() -> None:
    record = _fixture("openai_responses_mil.json")
    record["output"]["outputBodyJson"]["usage"]["input_tokens_details"]["cached_tokens"] = 5
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 7
    assert record["input"]["cacheReadInputTokenCount"] == 5


@pytest.mark.parametrize("stream", [[{"type": "chunk"}], []])
async def test_non_openai_stream_with_input_request_is_not_responses(
    stream: list[dict[str, Any]],
) -> None:
    record = {
        "input": {"inputBodyJson": _MIN_RESPONSES_REQUEST},
        "output": {"outputBodyJson": stream},
    }
    await normalize_record(record, config=_CONFIG)
    assert record["_parsed_as"] != "openai-responses-stream"


# --------------------------------------------------------------------------
# OpenAI Chat Completions
# --------------------------------------------------------------------------

_CHAT_FIXTURES = [
    ("openai_chat_trivial_mil.json", "openai-chat"),
    ("openai_chat_tool_call_mil.json", "openai-chat"),
    ("openai_chat_large_prompt_repeat_cached_mil.json", "openai-chat"),
    ("openai_chat_trivial_stream_mil.json", "openai-chat-stream"),
    ("openai_chat_tool_call_stream_mil.json", "openai-chat-stream"),
    ("openai_chat_thinking_high_stream_mil.json", "openai-chat-stream"),
    ("invoke_pixtral_chat_mil.json", "openai-chat"),
    ("invoke_pixtral_chat_stream_mil.json", "openai-chat-stream"),
]


@pytest.mark.parametrize("fixture,expected", _CHAT_FIXTURES)
async def test_openai_chat_sets_parsed_as(fixture: str, expected: str) -> None:
    record = _fixture(fixture)
    normalized = await normalize_record(record, config=_CONFIG)
    assert record["_parsed_as"] == expected
    assert normalized.output.message is not None


@pytest.mark.parametrize("fixture", [f for f, _ in _CHAT_FIXTURES])
def test_openai_chat_never_matches_earlier_formats(fixture: str) -> None:
    record = _fixture(fixture)
    in_body = record["input"]["inputBodyJson"]
    out_body = record["output"]["outputBodyJson"]
    for fmt in _FORMATS:
        if fmt.name.startswith("openai-chat"):
            continue
        with pytest.raises(ValidationError):
            fmt.request_adapter.validate_python(in_body)
            fmt.response_adapter.validate_python(out_body)


@pytest.mark.parametrize(
    "fixture", ["openai_responses_mil.json", "openai_responses_stream_mil.json"]
)
async def test_responses_records_are_not_taken_for_chat(fixture: str) -> None:
    record = _fixture(fixture)
    await normalize_record(record, config=_CONFIG)
    assert not record["_parsed_as"].startswith("openai-chat")


async def test_openai_chat_overwrites_tokens_with_additive_split() -> None:
    record = _fixture("openai_chat_trivial_mil.json")
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 13
    assert record["input"]["cacheReadInputTokenCount"] == 0
    assert record["input"]["cacheWriteInputTokenCount"] == 0
    assert record["output"]["outputTokenCount"] == 11
    assert record["output"]["reasoningTokenCount"] == 19


async def test_openai_chat_stream_overwrites_tokens_from_usage_chunk() -> None:
    record = _fixture("openai_chat_trivial_stream_mil.json")
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 13
    assert record["output"]["outputTokenCount"] + record["output"]["reasoningTokenCount"] == 29


async def test_openai_chat_cached_prompt_tokens() -> None:
    record = _fixture("openai_chat_large_prompt_repeat_cached_mil.json")
    await normalize_record(record, config=_CONFIG)
    assert record["input"]["inputTokenCount"] == 2
    assert record["input"]["cacheReadInputTokenCount"] == 24316


async def test_openai_chat_rejected_request_stays_unknown() -> None:
    record = _fixture("openai_chat_rejected_mil.json")
    normalized = await normalize_record(record, config=_CONFIG)
    assert record["_parsed_as"] == "unknown"
    assert normalized.output.message is None


@pytest.mark.parametrize(
    ("fixture", "name", "media_type"),
    [
        ("openai_chat_image_data_url_mil.json", "image.png", "image/png"),
        ("openai_chat_image_data_url_stream_mil.json", "image.png", "image/png"),
        ("openai_chat_file_pdf_data_mil.json", "secret.pdf", "application/pdf"),
    ],
)
async def test_openai_chat_inline_attachments_are_hashed(
    fixture: str, name: str, media_type: str
) -> None:
    record = _fixture(fixture)
    normalized = await normalize_record(record, config=_CONFIG)
    (file,) = normalized.accessed_files
    assert (file.name, file.media_type, file.provenance) == (name, media_type, "attachment")
    assert set(file.content_hashes or {}) == {"sha256", "sha1", "md5"}
    assert file.byte_length


async def test_openai_chat_without_attachments_reports_no_files() -> None:
    normalized = await normalize_record(_fixture("openai_chat_trivial_mil.json"), config=_CONFIG)
    assert normalized.accessed_files == []


@pytest.mark.parametrize(
    ("fixture", "operation"),
    [
        ("invoke_pixtral_chat_mil.json", "InvokeModel"),
        ("invoke_pixtral_chat_stream_mil.json", "InvokeModelWithResponseStream"),
    ],
)
async def test_invoke_model_chat_bodies_carry_the_answer(fixture: str, operation: str) -> None:
    record = _fixture(fixture)
    assert record["operation"] == operation
    normalized = await normalize_record(record, config=_CONFIG)
    assert normalized.output.message is not None
    assert normalized.output.stop_reason == "end_turn"
    assert [b.text for b in normalized.output.message.content] == ["Hi there!"]
