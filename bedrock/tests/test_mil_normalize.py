"""Tests for the MIL record dispatcher producing NormalizedInvocation."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from slashid_bedrock_forwarder.mil_normalize import normalize_record


# Minimal valid request bodies for each format — both request and response
# must validate, so every test needs an inputBodyJson that satisfies the
# request-side schema for the format under test.
_MIN_ANTHROPIC_REQUEST: dict[str, Any] = {
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
}
_MIN_CONVERSE_REQUEST: dict[str, Any] = {
    "messages": [{"role": "user", "content": [{"text": "hi"}]}],
}


def test_already_converse_shape_passes_through() -> None:
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
    normalized = normalize_record(record)
    # Raw input body is preserved verbatim — no rewrite.
    assert record["input"]["inputBodyJson"] == {
        "toolConfig": {"tools": []},
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
    }
    assert record["_parsed_as"] == "bedrock-converse"
    assert normalized.output is not None
    assert normalized.output.stop_reason == "end_turn"


def test_anthropic_tools_rewritten_to_toolconfig() -> None:
    """Tools normalization runs when both request and response identify the
    record as Anthropic — either the non-streaming dict here, or a streaming
    events list (covered by the stream tests below).

    ``_rewrite_input_tools`` still mutates ``record["input"]["inputBodyJson"]``
    on-record, because ``events._available_tools(record)`` reads from there.
    """
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
    normalize_record(record)
    body: Any = record["input"]["inputBodyJson"]
    tools = body["toolConfig"]["tools"]
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
    normalized = normalize_record(record)
    assert normalized.output is not None
    assert normalized.output.stop_reason == "end_turn"
    assert normalized.output.message is not None
    text_parts = [c.text for c in normalized.output.message.content if c.kind == "text"]
    assert "".join(t or "" for t in text_parts) == "hello world"


def test_anthropic_stream_tool_use_block_reconstructed() -> None:
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
    normalized = normalize_record(record)
    assert normalized.output is not None
    assert normalized.output.stop_reason == "tool_use"
    assert normalized.output.message is not None
    tool_uses = [c for c in normalized.output.message.content if c.kind == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0].tool_use_id == "toolu-1"
    assert tool_uses[0].tool_name == "Bash"
    assert tool_uses[0].tool_input == {"cmd": "ls"}


def test_malformed_tool_input_json_does_not_leak_to_logs(caplog: pytest.LogCaptureFixture) -> None:
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
    normalize_record(record)

    relevant = [r for r in caplog.records if "tool_use input_json malformed" in r.getMessage()]
    assert relevant, "expected the malformed-input warning to fire"
    for r in relevant:
        msg = r.getMessage()
        # The sensitive content must not appear.
        assert "rm -rf" not in msg
        assert "secret" not in msg


def test_anthropic_nonstreaming_response_rewritten() -> None:
    """Non-streaming InvokeModel-against-Anthropic response reaches
    canonical NormalizedInvocation shape.

    Regression: before the fix, `_used_tools` (which reads from
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
    normalized = normalize_record(record)

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


def test_anthropic_nonstreaming_missing_type_field_not_touched() -> None:
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
    normalize_record(record)
    # Raw body is preserved — we no longer mutate it.
    assert record["output"]["outputBodyJson"] == ambiguous
    # Neither request+response pair matches → unknown.
    assert record["_parsed_as"] == "unknown"


def test_anthropic_nonstreaming_thinking_block_folded_into_text() -> None:
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
    normalized = normalize_record(record)
    assert normalized.output is not None
    assert normalized.output.message is not None
    content = normalized.output.message.content
    # thinking → reasoning kind; text → text kind.
    assert [(c.kind, c.text) for c in content] == [
        ("reasoning", "reasoning..."),
        ("text", "answer"),
    ]


def test_converse_response_not_reprocessed() -> None:
    """A Converse-shape response passes through untouched — raw body preserved."""
    original = {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
    }
    record = {
        "input": {"inputBodyJson": dict(_MIN_CONVERSE_REQUEST)},
        "output": {"outputBodyJson": dict(original)},
    }
    normalize_record(record)
    # Raw output body preserved (no in-place rewrite).
    assert record["output"]["outputBodyJson"] == original
    assert record["_parsed_as"] == "bedrock-converse"


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
        "input": {
            "inputBodyJson": {
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                "tools": [{"name": "x"}],
            }
        },
        "output": {"outputBodyJson": list(nova_stream)},
    }
    normalize_record(record)
    # Output is untouched — we don't claim ownership of this shape.
    assert record["output"]["outputBodyJson"] == nova_stream
    # Input tools are also untouched (dispatch fell through — parsed_as="unknown").
    assert "toolConfig" not in record["input"]["inputBodyJson"]
    assert record["_parsed_as"] == "unknown"


def test_anthropic_message_backfills_missing_top_level_tokens() -> None:
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
    normalized = normalize_record(record)
    # Input tools rewritten on-record because output identified as Anthropic stream.
    assert "toolConfig" in record["input"]["inputBodyJson"]
    # NormalizedInvocation carries the reconstructed stop reason.
    assert normalized.output is not None
    assert normalized.output.stop_reason == "end_turn"


# --------------------------------------------------------------------------
# Table-driven dispatch: parsed_as marker + fallthrough
# --------------------------------------------------------------------------


def test_dispatch_anthropic_message_sets_parsed_as() -> None:
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
    normalize_record(record)
    assert record["_parsed_as"] == "anthropic-message"


def test_dispatch_anthropic_stream_sets_parsed_as() -> None:
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
    normalize_record(record)
    assert record["_parsed_as"] == "anthropic-stream"


def test_dispatch_native_converse_sets_parsed_as() -> None:
    record = {
        "input": {"inputBodyJson": {"messages": [{"role": "user", "content": [{"text": "hi"}]}]}},
        "output": {
            "outputBodyJson": {
                "output": {"message": {"role": "assistant", "content": [{"text": "hello"}]}},
                "stopReason": "end_turn",
            }
        },
    }
    normalize_record(record)
    assert record["_parsed_as"] == "bedrock-converse"


def test_dispatch_unknown_shape_marks_parsed_as_unknown_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record = {
        "modelId": "amazon.new-model-v1:0",
        "requestId": "req-xyz",
        "input": {"inputBodyJson": {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}},
        "output": {"outputBodyJson": {"totally": "unknown", "shape": [1, 2, 3]}},
    }
    with caplog.at_level(logging.WARNING, logger="slashid_bedrock_forwarder.mil_normalize"):
        normalize_record(record)
    assert record["_parsed_as"] == "unknown"
    assert any(
        "unrecognized MIL body shape" in r.message and "amazon.new-model-v1:0" in r.message
        for r in caplog.records
    )


def test_dispatch_backfill_tokens_from_anthropic_message_usage() -> None:
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
    normalize_record(record)
    assert record["input"]["cacheWriteInputTokenCount"] == 42
    assert record["input"]["inputTokenCount"] == 10
    assert record["output"]["outputTokenCount"] == 5


def test_dispatch_backfill_tokens_idempotent_does_not_overwrite() -> None:
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
    normalize_record(record)
    assert record["input"]["inputTokenCount"] == 999


# --------------------------------------------------------------------------
# Structural-exclusivity invariant: exactly one format's response_adapter
# matches each canonical payload. Guards against a future schema loosening
# that would let two formats claim the same shape.
# --------------------------------------------------------------------------


from pydantic import ValidationError  # noqa: E402

from slashid_bedrock_forwarder.mil_normalize import _FORMATS  # noqa: E402


@pytest.mark.parametrize(
    "expected_name,payload",
    [
        (
            "anthropic-message",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            },
        ),
        (
            "anthropic-stream",
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
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
            },
        ),
    ],
)
def test_format_structural_exclusivity(expected_name: str, payload: Any) -> None:
    matches = []
    for fmt in _FORMATS:
        try:
            fmt.response_adapter.validate_python(payload)
        except ValidationError:
            continue
        matches.append(fmt.name)
    assert matches == [expected_name], (
        f"Expected exactly {[expected_name]!r} to match, got {matches!r}. "
        "Two formats claiming the same shape violates first-match-wins invariance."
    )


# --------------------------------------------------------------------------
# End-to-end: parsed_as flows into AIInvocationObservedV1
# --------------------------------------------------------------------------


async def test_e2e_unrecognized_shape_emits_parsed_as_unknown() -> None:
    """Full pipeline: unknown-format MIL record → normalize_record marks it
    with _parsed_as="unknown" → build_event surfaces it on the wire event.
    Identity, model, tokens survive; semantic fields (stop_reason, tools)
    are None/empty."""
    from slashid_ai_forwarder_core.events import build_event

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
    normalized = normalize_record(record)
    event = await build_event(normalized, record)
    assert event is not None
    assert event.parsed_as == "unknown"
    # Semantic fields empty/None on unknown-shape records.
    assert event.stop_reason is None
    assert event.used_tools is None
    assert event.available_tools is None
    # Model + identity + tokens survive from the envelope.
    assert event.model.raw_model_id == "amazon.hypothetical-model-v1:0"
    assert event.tokens.input == 5
    assert event.tokens.output == 3


async def test_e2e_anthropic_message_sets_parsed_as() -> None:
    """Happy path: Anthropic-message record → parsed_as="anthropic-message"."""
    from slashid_ai_forwarder_core.events import build_event

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
    normalized = normalize_record(record)
    event = await build_event(normalized, record)
    assert event is not None
    assert event.parsed_as == "anthropic-message"


async def test_e2e_converse_response_sets_parsed_as() -> None:
    """Happy path: native Converse response → parsed_as="bedrock-converse"."""
    from slashid_ai_forwarder_core.events import build_event

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
    normalized = normalize_record(record)
    event = await build_event(normalized, record)
    assert event is not None
    assert event.parsed_as == "bedrock-converse"
