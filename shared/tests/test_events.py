"""Unit tests for the pure transformation logic in `events`.

Envelope-derivation tests (identity extraction, model catalog lookup,
stop-reason coercion, request-id drop, timestamp normalisation) live in
``bedrock/tests/test_event_envelope.py`` — they exercise the Bedrock
vendor half. Here we test only the pure
``build_event_from_normalized`` shared builder, feeding it an ad-hoc
``EventEnvelope`` when a record-driven fixture is convenient.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

import pytest

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIInvocationTokens,
    AIModel,
    AWSIdentityDetails,
    EventEnvelope,
    GCPIdentityDetails,
    build_event_from_normalized,
    build_sparse_event,
    parse_tool_name,
    redact_for_logging,
)
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    anthropic_dict_to_normalized,
)
from slashid_ai_forwarder_core.normalize.converse.normalize import (
    converse_dict_to_normalized,
)
from slashid_ai_forwarder_core.normalize.normalized.tools import build_tools_declared
from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)


def _config(
    *,
    include_raw_content: bool = False,
    max_content_size: int = 100_000,
    input_scope: Literal["session", "round"] = "round",
    round_link_depth: int = 10,
) -> BaseConfig:
    return BaseConfig(
        endpoint="http://test",
        push_token="test",
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
        input_scope=input_scope,
        round_link_depth=round_link_depth,
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
async def test_parse_tool_name(name: str, expected: tuple[str, str, str]) -> None:
    assert parse_tool_name(name) == expected


def _mil_record(**overrides: Any) -> dict[str, Any]:
    """Bedrock-shaped fixture used only to feed ``_envelope`` below.

    Kept here (rather than imported from bedrock/) so shared tests stay
    self-contained: the pure shared builder should work off any
    ``EventEnvelope`` regardless of how it was constructed.
    """
    base: dict[str, Any] = {
        "requestId": "req-1",
        "timestamp": "2026-06-01T12:00:00Z",
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "accountId": "123456789012",
        "identity": {
            "arn": "arn:aws:iam::123456789012:user/alice",
            "accessKeyId": "AKIAEXAMPLE",
        },
        "input": {"inputTokenCount": 100, "cacheReadInputTokenCount": 5},
        "output": {
            "outputTokenCount": 50,
            "outputBodyJson": {
                "stopReason": "end_turn",
                "output": {"message": {"role": "assistant", "content": []}},
            },
        },
    }
    base.update(overrides)
    return base


def _envelope(record: dict[str, Any]) -> EventEnvelope:
    """Ad-hoc ``EventEnvelope`` built from a Bedrock-shaped fixture.

    Test-side duplicate of bedrock/event_envelope.py's extraction so the
    shared suite has no dependency on the bedrock package. Fewer branches
    than the real thing: assumes ``requestId`` and ``identity.arn`` are
    both present — record-drop-on-missing-* behaviour is tested in
    bedrock/tests/test_event_envelope.py against the real function.
    """
    ident = record.get("identity") or {}
    principal = ident.get("resolved_arn") or ident.get("arn") or ""
    identity = AWSIdentityDetails(
        principal_arn=principal,
        access_key_id=ident.get("accessKeyId") or None,
    )
    inp = record.get("input") or {}
    out = record.get("output") or {}
    raw_model_id = str(record.get("modelId") or "")

    return EventEnvelope(
        request_id=str(record["requestId"]),
        timestamp=record.get("timestamp", "2026-06-01T12:00:00+00:00"),
        identity_details=identity,
        model=AIModel(id=raw_model_id, raw_model_id=raw_model_id or None),
        tokens=AIInvocationTokens(
            input=int(inp.get("inputTokenCount") or 0),
            output=int(out.get("outputTokenCount") or 0),
            cache_read=int(inp.get("cacheReadInputTokenCount") or 0),
            cache_write=int(inp.get("cacheWriteInputTokenCount") or 0),
            reasoning=0,
        ),
        parsed_as=record.get("_parsed_as", "unknown"),
    )


async def test_build_event_minimal() -> None:
    record = _mil_record()
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert isinstance(event, AIInvocationObservedV1)
    assert event.request_id == "req-1"
    assert isinstance(event.identity_details, AWSIdentityDetails)
    assert event.identity_details.principal_arn == "arn:aws:iam::123456789012:user/alice"
    assert event.identity_details.access_key_id == "AKIAEXAMPLE"
    assert event.model.id == "us.anthropic.claude-sonnet-4-6"
    assert event.tokens.input == 100
    assert event.tokens.output == 50
    assert event.tokens.cache_read == 5
    assert event.tokens.cache_write == 0
    assert event.stop_reason == "end_turn"
    # Optional fields stay None when not populated.
    assert event.available_tool_servers is None
    assert event.available_tools is None
    assert event.used_tools is None
    assert event.available_agents is None
    assert event.used_agent_ids is None


async def test_build_event_with_tools_and_used_ids() -> None:
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
                },
                "messages": [],
            },
        },
        output={
            "outputTokenCount": 50,
            "outputBodyJson": {
                "stopReason": "tool_use",
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "name": "mcp__excalidraw__create_view",
                                    "input": {},
                                }
                            }
                        ],
                    }
                },
            },
        },
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.available_tool_servers is not None
    assert event.available_tools is not None

    servers = {s.name: s for s in event.available_tool_servers}
    assert servers["excalidraw"].kind == "mcp"
    assert servers["builtin"].kind == "runtime"

    tools_by_name = {t.name: t for t in event.available_tools}
    assert tools_by_name["create_view"].tool_server_id == servers["excalidraw"].id
    assert tools_by_name["Bash"].tool_server_id == servers["builtin"].id

    # Output-only tool_use with no matching tool_result → deferred to the
    # invocation event where the result actually shows up. Nothing to emit
    # here.
    assert event.used_tools is None
    assert event.stop_reason == "tool_use"


def _record_with_bash(
    *,
    input_messages: list[dict[str, Any]] | None = None,
    output_content: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal Converse-shape MIL record advertising a single `Bash` tool.

    Pair with ``await converse_dict_to_normalized(record)``.
    """
    body: dict[str, Any] = {
        "toolConfig": {"tools": [{"toolSpec": {"name": "Bash", "description": "shell"}}]},
        "messages": input_messages if input_messages is not None else [],
    }
    output_body: dict[str, Any] = {"stopReason": "tool_use"}
    if output_content is not None:
        output_body["output"] = {"message": {"role": "assistant", "content": output_content}}
    return _mil_record(
        input={"inputTokenCount": 10, "inputBodyJson": body},
        output={"outputTokenCount": 5, "outputBodyJson": output_body},
    )


def _anthropic_record_with_bash(
    *,
    input_messages: list[dict[str, Any]] | None = None,
    output_content: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal Anthropic-shape MIL record advertising a single `Bash` tool.

    Pair with ``await anthropic_dict_to_normalized(record)``. Uses Anthropic
    request-side ``tools`` (not Converse ``toolConfig``) and produces an
    ``AnthropicMessage`` response envelope.
    """
    body: dict[str, Any] = {
        "tools": [{"name": "Bash", "description": "shell"}],
        "messages": input_messages if input_messages is not None else [],
    }
    output_body: dict[str, Any] = {
        "type": "message",
        "role": "assistant",
        "content": output_content or [],
        "stop_reason": "tool_use",
    }
    return _mil_record(
        input={"inputTokenCount": 10, "inputBodyJson": body},
        output={"outputTokenCount": 5, "outputBodyJson": output_body},
    )


async def test_used_tools_converse_error_status_maps_to_is_error() -> None:
    """Converse `toolResult.status == "error"` propagates as is_error=True."""
    record = _record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "tu_1", "name": "Bash", "input": {}}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_1",
                            "status": "error",
                            "content": [{"text": "boom"}],
                        }
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.used_tools is not None
    assert len(event.used_tools) == 1
    assert event.used_tools[0].is_error is True
    assert event.used_tools[0].trace_id is None


async def test_used_tools_anthropic_shape_is_error_flag() -> None:
    """Anthropic `tool_result.is_error: true` propagates."""
    record = _anthropic_record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_2",
                        "is_error": True,
                        "content": "boom",
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await anthropic_dict_to_normalized(record), _envelope(record), config=_config()
    )
    assert event.used_tools is not None
    assert event.used_tools[0].is_error is True


async def test_used_tools_extracts_trace_id_from_converse_json_block() -> None:
    """Bedrock Converse structured_content passthrough → `{json: {$opentelemetry: {...}}}`."""
    trace_id = "a" * 32
    record = _record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "tu_3", "name": "Bash", "input": {}}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_3",
                            "status": "success",
                            "content": [
                                {
                                    "json": {
                                        "flow": "gate_svid",
                                        "$opentelemetry": {
                                            "trace_id": trace_id,
                                            "span_id": "1" * 16,
                                        },
                                    }
                                }
                            ],
                        }
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.used_tools is not None
    assert event.used_tools[0].trace_id == trace_id
    assert event.used_tools[0].span_id == "1" * 16
    assert event.used_tools[0].is_error is False


async def test_used_tools_extracts_trace_id_from_stringified_structured_content() -> None:
    """Clients that stringify structuredContent land the block in a text block."""
    trace_id = "b" * 32
    envelope = json.dumps(
        {
            "flow": "gate_svid",
            "$opentelemetry": {"trace_id": trace_id, "span_id": "2" * 16},
        }
    )
    record = _record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "tu_4", "name": "Bash", "input": {}}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_4",
                            "status": "success",
                            "content": [{"text": envelope}],
                        }
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.used_tools is not None
    assert event.used_tools[0].trace_id == trace_id
    assert event.used_tools[0].span_id == "2" * 16


async def test_used_tools_extracts_trace_id_from_anthropic_string_content() -> None:
    """Anthropic tool_result.content can be a bare JSON string — parse it too."""
    trace_id = "c" * 32
    envelope = json.dumps(
        {"$opentelemetry": {"trace_id": trace_id, "span_id": "3" * 16}, "flow": "x"}
    )
    record = _anthropic_record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_5", "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_5",
                        "content": envelope,
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await anthropic_dict_to_normalized(record), _envelope(record), config=_config()
    )
    assert event.used_tools is not None
    assert event.used_tools[0].trace_id == trace_id
    assert event.used_tools[0].span_id == "3" * 16


async def test_used_tools_propagates_tool_use_id_across_vendors() -> None:
    """Same tool_use_id → wire, regardless of vendor. Pre-Phase-2 this test
    combined both shapes in one record; post-Phase-2 a real record is one
    vendor or the other, so verify each independently."""
    anthropic_record = _anthropic_record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_anth", "name": "Bash", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_anth", "content": "ok"},
                ],
            },
        ],
    )
    converse_record = _record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "tu_conv", "name": "Bash", "input": {}}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_conv",
                            "status": "success",
                            "content": [{"text": "ok"}],
                        }
                    },
                ],
            },
        ],
    )
    a_event = await build_event_from_normalized(
        await anthropic_dict_to_normalized(anthropic_record),
        _envelope(anthropic_record),
        config=_config(),
    )
    c_event = await build_event_from_normalized(
        await converse_dict_to_normalized(converse_record, config=_config()),
        _envelope(converse_record),
        config=_config(),
    )
    assert a_event.used_tools is not None
    assert c_event.used_tools is not None
    assert a_event.used_tools[0].tool_use_id == "toolu_anth"
    assert c_event.used_tools[0].tool_use_id == "tu_conv"


async def test_used_tools_extracts_otel_from_text_marker_on_error() -> None:
    """On error paths clients drop structured content — OTel context survives as text marker.

    mcp-gate-demo's error path stamps `[trace_id=<hex> span_id=<hex>]` at
    the end of the text block precisely because Claude Code on Bedrock
    forwards the error string only, discarding structuredContent.
    """
    trace_id = "f" * 32
    span_id = "6" * 16
    err_text = f"McpError: Internal error: 403 Forbidden\n[trace_id={trace_id} span_id={span_id}]"
    record = _anthropic_record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_err", "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_err",
                        "is_error": True,
                        "content": err_text,
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await anthropic_dict_to_normalized(record), _envelope(record), config=_config()
    )
    assert event.used_tools is not None
    assert event.used_tools[0].is_error is True
    assert event.used_tools[0].trace_id == trace_id
    assert event.used_tools[0].span_id == span_id


async def test_used_tools_missing_status_defaults_to_success() -> None:
    """Converse `status` is optional (only Claude 3 sets it) — absent = success."""
    record = _record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "tu_ns", "name": "Bash", "input": {}}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_ns",
                            "content": [{"text": "ok"}],
                        }
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.used_tools is not None
    assert event.used_tools[0].is_error is False


async def test_used_tools_anthropic_missing_is_error_defaults_to_success() -> None:
    """Anthropic `is_error` is optional — absent = success."""
    record = _anthropic_record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_na", "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_na", "content": "ok"},
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await anthropic_dict_to_normalized(record), _envelope(record), config=_config()
    )
    assert event.used_tools is not None
    assert event.used_tools[0].is_error is False


async def test_used_tools_defers_output_tool_use_without_result() -> None:
    """Emit the completed prior-turn call; defer the new tool_use in output."""
    trace_id = "d" * 32
    record = _record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "tu_prev", "name": "Bash", "input": {}}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_prev",
                            "status": "success",
                            "content": [
                                {
                                    "json": {
                                        "$opentelemetry": {
                                            "trace_id": trace_id,
                                            "span_id": "4" * 16,
                                        }
                                    }
                                }
                            ],
                        }
                    }
                ],
            },
        ],
        output_content=[
            {"toolUse": {"toolUseId": "tu_new", "name": "Bash", "input": {}}},
        ],
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.used_tools is not None
    # Only the pair-complete entry emits. tu_new (output-only) waits for its
    # result on a future invocation event.
    assert len(event.used_tools) == 1
    assert event.used_tools[0].is_error is False
    assert event.used_tools[0].trace_id == trace_id


async def test_used_tools_skips_result_without_matching_tool_use() -> None:
    """A tool_result whose tool_use_id we can't resolve is dropped, not stubbed."""
    record = _record_with_bash(
        input_messages=[
            # no assistant tool_use for tu_orphan visible in this record
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_orphan",
                            "status": "success",
                            "content": [{"text": "hi"}],
                        }
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.used_tools is None


async def test_used_tools_ignores_prior_tool_results_before_last_assistant() -> None:
    """tool_result blocks that pre-date the last assistant turn were already reported."""
    record = _record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "tu_old", "name": "Bash", "input": {}}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_old",
                            "status": "error",
                            "content": [{"text": "old boom"}],
                        }
                    }
                ],
            },
            # A second assistant turn comes after — the tu_old result is now
            # "history" and shouldn't be re-emitted.
            {"role": "assistant", "content": [{"text": "ok, next"}]},
            {"role": "user", "content": [{"text": "continue"}]},
        ],
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.used_tools is None


async def test_used_tools_wire_form_matches_new_schema() -> None:
    """Wire dump uses `used_tools` with the AIToolUse item shape."""
    record = _record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "tu_w", "name": "Bash", "input": {}}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tu_w",
                            "status": "error",
                            "content": [
                                {
                                    "json": {
                                        "$opentelemetry": {
                                            "trace_id": "e" * 32,
                                            "span_id": "5" * 16,
                                        }
                                    }
                                }
                            ],
                        }
                    }
                ],
            },
        ],
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    assert event.used_tools is not None
    tool_id = event.used_tools[0].tool_id
    wire = event.model_dump(mode="json", exclude_none=True)
    assert "used_tool_ids" not in wire
    assert wire["used_tools"] == [
        {
            "tool_id": tool_id,
            "tool_use_id": "tu_w",
            "is_error": True,
            "trace_id": "e" * 32,
            "span_id": "5" * 16,
        }
    ]


async def test_tool_id_differs_for_different_schema() -> None:
    """Same tool name with different input schemas → different tool IDs."""

    def _record_with_schema(schema: dict[str, Any]) -> dict[str, Any]:
        return _mil_record(
            input={
                "inputTokenCount": 1,
                "inputBodyJson": {
                    "toolConfig": {
                        "tools": [
                            {"toolSpec": {"name": "WebFetch", "inputSchema": {"json": schema}}}
                        ]
                    },
                    "messages": [],
                },
            }
        )

    r1 = _record_with_schema({"type": "object", "properties": {"url": {"type": "string"}}})
    r2 = _record_with_schema(
        {
            "type": "object",
            "properties": {"url": {"type": "string"}, "depth": {"type": "integer"}},
        }
    )
    ev1 = await build_event_from_normalized(
        await converse_dict_to_normalized(r1, config=_config()), _envelope(r1), config=_config()
    )
    ev2 = await build_event_from_normalized(
        await converse_dict_to_normalized(r2, config=_config()), _envelope(r2), config=_config()
    )
    assert ev1.available_tools is not None and ev2.available_tools is not None
    assert ev1.available_tools[0].id != ev2.available_tools[0].id


async def test_tool_id_differs_for_different_description() -> None:
    """Same tool name with different description → different tool IDs."""

    def _record_with_desc(desc: str) -> dict[str, Any]:
        return _mil_record(
            input={
                "inputTokenCount": 1,
                "inputBodyJson": {
                    "toolConfig": {"tools": [{"toolSpec": {"name": "Bash", "description": desc}}]},
                    "messages": [],
                },
            }
        )

    r1 = _record_with_desc("Run a shell command")
    r2 = _record_with_desc("Execute arbitrary shell commands with elevated privileges")
    ev1 = await build_event_from_normalized(
        await converse_dict_to_normalized(r1, config=_config()), _envelope(r1), config=_config()
    )
    ev2 = await build_event_from_normalized(
        await converse_dict_to_normalized(r2, config=_config()), _envelope(r2), config=_config()
    )
    assert ev1.available_tools is not None and ev2.available_tools is not None
    assert ev1.available_tools[0].id != ev2.available_tools[0].id


async def test_invalid_stop_reason_literal_rejected_on_construction() -> None:
    """Pydantic Literal type rejects values outside the AIStopReason enum."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AIInvocationObservedV1(
            request_id="r",
            timestamp="t",
            identity_details=AWSIdentityDetails(principal_arn="arn:aws:iam::1:user/x"),
            model=AIModel(id="m"),
            parsed_as="anthropic-message",
            stop_reason="not-a-real-reason",  # ty: ignore[invalid-argument-type]
        )


def test_aws_identity_details_kind_defaults_to_aws() -> None:
    """Discriminator field for the future GCPIdentityDetails union sibling.

    Construction without an explicit `kind` gets the default "aws". Every
    wire-emitted Bedrock event carries `"kind": "aws"` after this change.
    """
    ident = AWSIdentityDetails(principal_arn="arn:aws:iam::1:user/x")
    assert ident.kind == "aws"
    dumped = ident.model_dump(mode="json", exclude_none=True)
    assert dumped == {"kind": "aws", "principal_arn": "arn:aws:iam::1:user/x"}


def test_aws_identity_details_kind_literal_enforced() -> None:
    """Only the literal "aws" is accepted for kind — protects the future
    discriminated union from Bedrock-side typos."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AWSIdentityDetails(
            kind="gcp",  # ty: ignore[invalid-argument-type]
            principal_arn="arn:aws:iam::1:user/x",
        )


def test_aws_identity_details_round_trip() -> None:
    """model_validate → model_dump is lossless; kind field survives."""
    raw = {"kind": "aws", "principal_arn": "arn:aws:iam::1:user/x", "access_key_id": "AKIA..."}
    ident = AWSIdentityDetails.model_validate(raw)
    assert ident.model_dump(mode="json", exclude_none=True) == raw


def test_ai_invocation_observed_v1_identity_details_discriminator() -> None:
    """AIInvocationObservedV1.identity_details is a discriminated (single-variant
    (AWSIdentityDetails | GCPIdentityDetails) union — validating a dict
    without `kind` fails, ensuring downstream replay/fixture tooling
    stays honest about the tag."""
    from pydantic import ValidationError

    # Constructed via the pydantic model — kind defaults from AWSIdentityDetails.
    event = AIInvocationObservedV1(
        request_id="r",
        timestamp="t",
        identity_details=AWSIdentityDetails(principal_arn="arn:x"),
        model=AIModel(id="m"),
        parsed_as="anthropic-message",
    )
    assert event.identity_details.kind == "aws"

    # Wire-form model_validate REQUIRES the tag — the discriminator won't
    # infer it. A serialized dict without "kind" fails.
    raw_no_kind: dict[str, Any] = {
        "request_id": "r",
        "timestamp": "t",
        "identity_details": {"principal_arn": "arn:x"},
        "model": {"id": "m"},
        "parsed_as": "anthropic-message",
    }
    with pytest.raises(ValidationError):
        AIInvocationObservedV1.model_validate(raw_no_kind)

    # With the tag, validate succeeds.
    raw_with_kind = {**raw_no_kind, "identity_details": {"kind": "aws", "principal_arn": "arn:x"}}
    reparsed = AIInvocationObservedV1.model_validate(raw_with_kind)
    assert isinstance(reparsed.identity_details, AWSIdentityDetails)
    assert reparsed.identity_details.principal_arn == "arn:x"


def test_event_envelope_identity_details_discriminator() -> None:
    """EventEnvelope carries the same discriminated identity_details union as
    AIInvocationObservedV1 — both widen together in Phase 3.1."""
    from pydantic import ValidationError

    env = EventEnvelope(
        request_id="r",
        timestamp="t",
        identity_details=AWSIdentityDetails(principal_arn="arn:x"),
        model=AIModel(id="m"),
        parsed_as="anthropic-message",
    )
    assert env.identity_details.kind == "aws"

    raw_no_kind: dict[str, Any] = {
        "request_id": "r",
        "timestamp": "t",
        "identity_details": {"principal_arn": "arn:x"},
        "model": {"id": "m"},
        "parsed_as": "anthropic-message",
    }
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(raw_no_kind)


def test_gcp_identity_details_defaults_to_kind_gcp() -> None:
    """Empty GCPIdentityDetails() serializes to {"kind": "gcp"} — matches
    the Vertex v1 wire shape when identity correlation is deferred."""
    identity = GCPIdentityDetails()
    assert identity.kind == "gcp"
    assert identity.model_dump(mode="json", exclude_none=True) == {"kind": "gcp"}


def test_identity_details_union_dispatches_on_kind() -> None:
    """model_validate on AIInvocationObservedV1 dispatches identity_details
    via the "kind" discriminator — GCPIdentityDetails path validates
    without an AWS principal_arn, AWSIdentityDetails path requires one."""
    raw_gcp = {
        "request_id": "r",
        "timestamp": "t",
        "identity_details": {"kind": "gcp"},
        "model": {"id": "m"},
        "parsed_as": "vertex-google",
    }
    event = AIInvocationObservedV1.model_validate(raw_gcp)
    assert isinstance(event.identity_details, GCPIdentityDetails)
    assert event.identity_details.credential_chain is None

    # AWS side still requires principal_arn — union stays strict per-variant.
    from pydantic import ValidationError

    raw_aws_missing_arn = {**raw_gcp, "identity_details": {"kind": "aws"}}
    with pytest.raises(ValidationError):
        AIInvocationObservedV1.model_validate(raw_aws_missing_arn)


def test_gcp_identity_details_wire_form_round_trips_on_envelope() -> None:
    """EventEnvelope accepts GCPIdentityDetails on the same union — Vertex's
    vertex_envelope constructor will populate it this way."""
    env = EventEnvelope(
        request_id="r",
        timestamp="t",
        identity_details=GCPIdentityDetails(),
        model=AIModel(id="publishers/google/models/gemini-2.5-flash"),
        parsed_as="vertex-google",
    )
    assert env.identity_details.kind == "gcp"
    wire = env.model_dump(mode="json", exclude_none=True)
    assert wire["identity_details"] == {"kind": "gcp"}


async def test_content_fields_default_to_hash_only() -> None:
    """include_raw_content=False (default): hash + mime + bytes, no text."""
    body = {"messages": [{"role": "user", "content": [{"text": "secret prompt"}]}]}
    record = _mil_record(
        input={"inputTokenCount": 1, "inputBodyJson": body},
        output={"outputTokenCount": 1, "outputBodyJson": {"stopReason": "end_turn"}},
    )
    normalized = await converse_dict_to_normalized(record, config=_config())
    event = await build_event_from_normalized(normalized, _envelope(record), config=_config())
    assert event.input is not None

    # Hash the canonical input serialization — same as build_event_from_normalized does:
    # the messages alone, as a JSON array.
    canonical_input = json.dumps(
        [m.model_dump(mode="json", exclude_none=True) for m in normalized.input.messages],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert event.input.content_hashes == {
        "sha256": hashlib.sha256(canonical_input).hexdigest(),
        "sha1": hashlib.sha1(canonical_input).hexdigest(),
        "md5": hashlib.md5(canonical_input).hexdigest(),
    }
    assert event.input.mime_type == "application/json"
    assert event.input.byte_length == len(canonical_input)
    # Crucially: no text.
    assert event.input.redacted_text is None


async def test_content_fields_include_raw_when_opted_in() -> None:
    body = {"messages": [{"role": "user", "content": [{"text": "hello"}]}]}
    record = _mil_record(
        input={"inputTokenCount": 1, "inputBodyJson": body},
        output={"outputTokenCount": 1, "outputBodyJson": {"stopReason": "end_turn"}},
    )
    normalized = await converse_dict_to_normalized(record, config=_config())
    event = await build_event_from_normalized(
        normalized, _envelope(record), config=_config(include_raw_content=True)
    )
    assert event.input is not None
    assert event.input.redacted_text is not None
    # Canonical serialization is deterministic; compare via re-serialization
    # (matches build_event_from_normalized: the messages alone, as an array).
    canonical = json.dumps(
        [m.model_dump(mode="json", exclude_none=True) for m in normalized.input.messages],
        sort_keys=True,
        separators=(",", ":"),
    )
    assert event.input.redacted_text == canonical


async def test_content_field_none_when_body_absent() -> None:
    record = _mil_record(input={"inputTokenCount": 1}, output={"outputTokenCount": 1})
    normalized = await converse_dict_to_normalized(record, config=_config())
    event = await build_event_from_normalized(normalized, _envelope(record), config=_config())
    # Input side: normalized.input is empty → model_dump produces {} → _build_content returns None.
    assert event.input is None
    # Output side: normalized.output.stop_reason defaults to "unknown" → dumps to
    # {"stop_reason": "unknown"} → _build_content returns a hash of that.
    assert event.output is not None
    assert event.output.content_hashes is not None


async def test_build_event_wire_form() -> None:
    record = _mil_record()
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        _envelope(record),
        config=_config(),
    )
    wire = event.model_dump(mode="json", exclude_none=True)
    # Spec dropped these; they must not show up in the JSON we send.
    assert "org_id" not in wire
    assert "connection_id" not in wire
    assert "identifier_from_source" not in wire
    assert "identity_source_type" not in wire
    # The new identity shape — `kind` discriminator prepared for the
    # future AWSIdentityDetails | GCPIdentityDetails union.
    assert wire["identity_details"] == {
        "kind": "aws",
        "principal_arn": "arn:aws:iam::123456789012:user/alice",
        "access_key_id": "AKIAEXAMPLE",
    }
    # No empty None placeholders for the rest.
    assert "available_tool_servers" not in wire
    assert "available_tools" not in wire
    assert "used_tools" not in wire
    assert "available_agents" not in wire
    # Tokens always present (default 0s).
    assert wire["tokens"] == {
        "input": 100,
        "output": 50,
        "cache_read": 5,
        "cache_write": 0,
        "reasoning": 0,
    }


@pytest.mark.asyncio
async def test_build_event_truncates_redacted_text() -> None:
    """redacted_text is capped to max_content_size characters."""
    record = _mil_record(
        input={
            "inputTokenCount": 10,
            "inputBodyJson": {"messages": [{"role": "user", "content": [{"text": "x" * 200}]}]},
        }
    )
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(
            record, config=_config(include_raw_content=True, max_content_size=20)
        ),
        _envelope(record),
        config=_config(include_raw_content=True, max_content_size=20),
    )
    assert event.input is not None
    assert event.input.redacted_text is not None
    assert len(event.input.redacted_text) <= 20
    assert "…" in event.input.redacted_text


def test_redact_for_logging_strips_input_and_output_redacted_text() -> None:
    payload = {
        "request_id": "r",
        "input": {
            "content_hashes": {"sha256": "abc"},
            "byte_length": 10,
            "redacted_text": "SECRET PROMPT",
        },
        "output": {
            "content_hashes": {"sha256": "def"},
            "redacted_text": "SECRET RESPONSE",
        },
    }
    redacted = redact_for_logging(payload)
    assert "redacted_text" not in redacted["input"]
    assert "redacted_text" not in redacted["output"]
    # Non-sensitive siblings survive.
    assert redacted["input"]["content_hashes"] == {"sha256": "abc"}
    assert redacted["input"]["byte_length"] == 10


def test_redact_for_logging_strips_accessed_files_redacted_content() -> None:
    payload = {
        "accessed_files": [
            {
                "name": "notes.txt",
                "content_hashes": {"sha256": "abc"},
                "byte_length": 11,
                "redacted_content": "hello world",
            },
            {"name": "doc.pdf", "content_hashes": {"sha256": "def"}},
        ]
    }
    redacted = redact_for_logging(payload)
    assert "redacted_content" not in redacted["accessed_files"][0]
    assert redacted["accessed_files"][0]["name"] == "notes.txt"
    assert redacted["accessed_files"][0]["content_hashes"] == {"sha256": "abc"}
    # Files without redacted_content pass through unchanged.
    assert redacted["accessed_files"][1] == {"name": "doc.pdf", "content_hashes": {"sha256": "def"}}


def test_redact_for_logging_leaves_input_untouched() -> None:
    """Returns a copy — the original payload isn't mutated. Callers can
    keep the wire-shape dict around for other purposes."""
    payload = {"input": {"redacted_text": "SECRET"}}
    redact_for_logging(payload)
    assert payload["input"]["redacted_text"] == "SECRET"


def test_redact_for_logging_walks_nested_lists() -> None:
    """redacted_content sitting deep inside a nested list-of-dicts still
    gets stripped — the recursive walk descends into lists."""
    payload = {
        "used_tools": [
            {"tool_id": "1", "extra": [{"redacted_content": "nested"}]},
        ],
    }
    redacted = redact_for_logging(payload)
    assert redacted["used_tools"][0]["extra"][0] == {}
    assert redacted["used_tools"][0]["tool_id"] == "1"


async def test_gcp_identity_details_credential_chain_empty() -> None:
    """Default construction: kind=gcp, credential_chain=None."""
    from slashid_ai_forwarder_core.events import GCPIdentityDetails

    ident = GCPIdentityDetails()
    assert ident.kind == "gcp"
    assert ident.credential_chain is None
    assert ident.model_dump(mode="json", exclude_none=True) == {"kind": "gcp"}


async def test_gcp_identity_details_length_1_chain_human() -> None:
    from slashid_ai_forwarder_core.events import GCPCredential, GCPIdentityDetails

    ident = GCPIdentityDetails(
        credential_chain=[
            GCPCredential(
                principal_email="alice@example.com",
                principal_subject="user:alice@example.com",
                oauth_client_id="32555940559.apps.googleusercontent.com",
            )
        ]
    )
    dumped = ident.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "kind": "gcp",
        "credential_chain": [
            {
                "principal_email": "alice@example.com",
                "principal_subject": "user:alice@example.com",
                "oauth_client_id": "32555940559.apps.googleusercontent.com",
            }
        ],
    }


async def test_gcp_identity_details_length_2_chain_impersonation() -> None:
    from slashid_ai_forwarder_core.events import GCPCredential, GCPIdentityDetails

    ident = GCPIdentityDetails(
        credential_chain=[
            GCPCredential(
                principal_email="alice@example.com",
                principal_subject="user:alice@example.com",
            ),
            GCPCredential(
                principal_email="sa@proj.iam.gserviceaccount.com",
                principal_subject="serviceAccount:sa@proj.iam.gserviceaccount.com",
            ),
        ]
    )
    dumped = ident.model_dump(mode="json", exclude_none=True)
    assert dumped["credential_chain"][0]["principal_email"] == "alice@example.com"
    assert dumped["credential_chain"][1]["principal_subject"].startswith("serviceAccount:")


async def test_gcp_identity_details_partial_credential_empty_position() -> None:
    """Partial attribution: root populated, tail empty → tail serializes to {}."""
    from slashid_ai_forwarder_core.events import GCPCredential, GCPIdentityDetails

    ident = GCPIdentityDetails(
        credential_chain=[
            GCPCredential(principal_email="alice@example.com"),
            GCPCredential(),  # all fields None
        ]
    )
    dumped = ident.model_dump(mode="json", exclude_none=True)
    assert dumped["credential_chain"][1] == {}
    assert dumped["credential_chain"][0]["principal_email"] == "alice@example.com"


async def test_gcp_identity_details_discriminated_union_still_works() -> None:
    from slashid_ai_forwarder_core.events import (
        AIInvocationObservedV1,
        GCPIdentityDetails,
    )

    ev = AIInvocationObservedV1.model_validate(
        {
            "request_id": "r1",
            "timestamp": "2026-09-09T12:00:00Z",
            "identity_details": {"kind": "gcp"},
            "model": {"id": "publishers/google/models/gemini-2.5-flash"},
            "parsed_as": "vertex-google",
        }
    )
    assert isinstance(ev.identity_details, GCPIdentityDetails)
    assert ev.identity_details.credential_chain is None


def _sparse_envelope(*, is_error: bool = False) -> EventEnvelope:
    """Minimal envelope for the sparse-builder / is_error tests below."""
    return EventEnvelope(
        request_id="r1",
        timestamp="2026-09-10T12:00:00+00:00",
        identity_details=GCPIdentityDetails(),
        model=AIModel(
            id="publishers/openai/models/gpt-oss-120b-maas",
            raw_model_id="publishers/openai/models/gpt-oss-120b-maas",
        ),
        parsed_as="vertex-audit",
        is_error=is_error,
    )


def test_build_sparse_event_all_conversation_fields_none() -> None:
    """``build_sparse_event`` carries envelope fields through and leaves
    every conversation-shaped field ``None`` — no ``NormalizedInvocation``
    involved, no post-hoc nulling."""
    event = build_sparse_event(_sparse_envelope(), config=_config())
    assert event.request_id == "r1"
    assert event.parsed_as == "vertex-audit"
    assert event.stop_reason is None
    assert event.input is None
    assert event.output is None
    assert event.used_tools is None
    assert event.available_tools is None
    assert event.available_tool_servers is None
    assert event.accessed_files is None


def test_build_sparse_event_is_error_sets_stop_reason_error() -> None:
    """Envelope-level error signal → wire ``stop_reason="error"``."""
    event = build_sparse_event(_sparse_envelope(is_error=True), config=_config())
    assert event.stop_reason == "error"
    # Other semantic fields still None.
    assert event.input is None
    assert event.output is None


async def test_build_event_from_normalized_is_error_overrides_stop_reason() -> None:
    """Even with a normalized invocation that carries a non-error
    ``stop_reason``, an errored envelope forces ``stop_reason="error"``.
    An errored request has no legit ``end_turn`` etc."""
    record = _mil_record()
    envelope = _envelope(record)
    envelope_error = envelope.model_copy(update={"is_error": True})
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        envelope_error,
        config=_config(),
    )
    # Fixture normalized has stop_reason="end_turn"; is_error wins.
    assert event.stop_reason == "error"


def test_build_sparse_event_passes_user_agent_through() -> None:
    """``user_agent`` is top-level on the wire event (a property of the
    request, not the principal) and threads through the sparse builder."""
    env = _sparse_envelope().model_copy(update={"user_agent": "curl/8.5.0,gzip(gfe)"})
    event = build_sparse_event(env, config=_config())
    assert event.user_agent == "curl/8.5.0,gzip(gfe)"


def test_build_sparse_event_user_agent_defaults_none() -> None:
    """Sources that carry no user agent (Bedrock MIL) leave it null."""
    assert build_sparse_event(_sparse_envelope(), config=_config()).user_agent is None


async def test_build_event_from_normalized_passes_user_agent_through() -> None:
    record = _mil_record()
    env = _envelope(record).model_copy(update={"user_agent": "google-cloud-sdk/1.2.3"})
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        env,
        config=_config(),
    )
    assert event.user_agent == "google-cloud-sdk/1.2.3"


def test_aws_identity_mfa_authenticated_is_tristate_placeholder() -> None:
    """``mfa_authenticated`` is a placeholder until a MIL x CloudTrail
    join lands. Tri-state: ``None`` means "not observed", distinct from
    an observed ``False``. ``exclude_none`` keeps it off the wire while
    unpopulated."""
    ident = AWSIdentityDetails(principal_arn="arn:aws:iam::123456789012:user/alice")
    assert ident.mfa_authenticated is None
    assert "mfa_authenticated" not in ident.model_dump(mode="json", exclude_none=True)

    observed_false = AWSIdentityDetails(
        principal_arn="arn:aws:iam::123456789012:user/alice",
        mfa_authenticated=False,
    )
    assert observed_false.model_dump(mode="json", exclude_none=True)["mfa_authenticated"] is False


def test_anthropic_identity_details_round_trips_through_the_union() -> None:
    from slashid_ai_forwarder_core.events import AnthropicIdentityDetails

    event = AIInvocationObservedV1.model_validate(
        {
            "request_id": "r",
            "timestamp": "2026-09-18T00:00:00Z",
            "identity_details": {"kind": "anthropic", "user_id": "user_01AbCdEfGhIjKlMnOpQrStUv"},
            "model": {"id": "claude-sonnet-5"},
            "parsed_as": "anthropic-inference-hook",
        }
    )
    assert isinstance(event.identity_details, AnthropicIdentityDetails)
    assert event.identity_details.user_id == "user_01AbCdEfGhIjKlMnOpQrStUv"
    assert event.model_dump(exclude_none=True)["identity_details"] == {
        "kind": "anthropic",
        "user_id": "user_01AbCdEfGhIjKlMnOpQrStUv",
    }


def test_anthropic_identity_details_requires_at_least_one_identifier() -> None:
    """Every identifier is optional on its own, because no single producer
    observes all of them — a hook inside the Claude application knows the
    acting user, an inline proxy knows only the key presented. But a payload
    naming nobody is rejected: there is nothing to resolve and nothing to
    meter, and the server rejects it too."""
    from pydantic import ValidationError

    from slashid_ai_forwarder_core.events import AnthropicIdentityDetails

    with pytest.raises(ValidationError):
        AnthropicIdentityDetails.model_validate({"kind": "anthropic"})


def test_anthropic_identity_details_accept_any_single_identifier() -> None:
    """A request authenticates as one principal, so several names for it is
    a producer that knows the same actor more than one way, not ambiguity."""
    from slashid_ai_forwarder_core.events import AnthropicIdentityDetails

    for field, value in (
        ("service_account_id", "svac_1"),
        ("user_id", "user_01A"),
        ("api_key_id", "apikey_1"),
        ("api_key_hash", "a" * 64),
    ):
        got = AnthropicIdentityDetails.model_validate({"kind": "anthropic", field: value})
        assert getattr(got, field) == value
    both = AnthropicIdentityDetails(service_account_id="svac_1", user_id="user_01A")
    assert both.service_account_id == "svac_1" and both.user_id == "user_01A"


def test_anthropic_identity_details_on_the_envelope() -> None:
    """EventEnvelope shares the union, so the receiver's envelope
    constructor populates it the same way bedrock/vertex do theirs."""
    from slashid_ai_forwarder_core.events import AnthropicIdentityDetails

    env = EventEnvelope(
        request_id="r",
        timestamp="2026-09-18T00:00:00Z",
        identity_details=AnthropicIdentityDetails(user_id="user_01Abc"),
        model=AIModel(id="claude-sonnet-5"),
        parsed_as="anthropic-inference-hook",
    )
    assert env.identity_details.kind == "anthropic"


async def test_envelope_conversation_id_reaches_the_event() -> None:
    """The envelope owns conversation_id; the shared builder carries it
    through unchanged. Bedrock and Vertex leave it unset and get None."""
    from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation

    env = EventEnvelope(
        request_id="r",
        timestamp="2026-09-18T00:00:00Z",
        identity_details=GCPIdentityDetails(),
        model=AIModel(id="claude-sonnet-5"),
        parsed_as="anthropic-inference-hook",
        conversation_id="00000006-0000-4000-8000-000000000000",
    )
    event = await build_event_from_normalized(NormalizedInvocation(), env, config=_config())
    assert event.conversation_id == "00000006-0000-4000-8000-000000000000"


def test_sparse_event_carries_conversation_id() -> None:
    """The compliance reader emits standalone denial events through the
    sparse builder; grouping an incident needs the conversation id there
    too."""
    env = _sparse_envelope()
    assert env.conversation_id is None
    event = build_sparse_event(env, config=_config())
    assert event.conversation_id is None
    env_with = env.model_copy(update={"conversation_id": "sess_1"})
    assert build_sparse_event(env_with, config=_config()).conversation_id == "sess_1"


def _msg(role: Literal["system", "user", "assistant"], text: str) -> NormalizedMessage:
    return NormalizedMessage(role=role, content=[NormalizedContent(kind="text", text=text)])


def _invocation(messages: list[NormalizedMessage], answer: str = "ok") -> NormalizedInvocation:
    return NormalizedInvocation(
        input=NormalizedInvocationInput(messages=messages),
        output=NormalizedInvocationOutput(
            message=_msg("assistant", answer), stop_reason="end_turn"
        ),
    )


def _plain_envelope() -> EventEnvelope:
    return EventEnvelope(
        request_id="r",
        timestamp="2026-06-01T12:00:00+00:00",
        identity_details=AWSIdentityDetails(principal_arn="arn:aws:iam::1:user/a"),
        model=AIModel(id="m"),
        parsed_as="test",
    )


def _canonical_sha256(body: object) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def test_round_scope_hashes_only_the_consumed_round() -> None:
    history = [_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]
    event = await build_event_from_normalized(
        _invocation(history), _plain_envelope(), config=_config()
    )
    assert event.input is not None and event.input.content_hashes is not None
    assert event.input.content_hashes["sha256"] == _canonical_sha256(
        [history[2].model_dump(mode="json", exclude_none=True)]
    )


async def test_session_scope_hashes_the_whole_transcript_as_a_message_list() -> None:
    history = [_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]
    event = await build_event_from_normalized(
        _invocation(history), _plain_envelope(), config=_config(input_scope="session")
    )
    assert event.input is not None and event.input.content_hashes is not None
    assert event.input.content_hashes["sha256"] == _canonical_sha256(
        [m.model_dump(mode="json", exclude_none=True) for m in history]
    )


async def test_a_leading_system_message_is_in_round_one_and_not_round_two() -> None:
    first = await build_event_from_normalized(
        _invocation([_msg("system", "sys"), _msg("user", "a")]), _plain_envelope(), config=_config()
    )
    second = await build_event_from_normalized(
        _invocation(
            [_msg("system", "sys"), _msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]
        ),
        _plain_envelope(),
        config=_config(),
    )
    assert first.input is not None and second.input is not None
    assert first.input.byte_length and second.input.byte_length
    assert first.input.byte_length > second.input.byte_length


async def test_input_does_not_depend_on_declared_tools() -> None:
    bare = _invocation([_msg("user", "a")])
    declared = _invocation([_msg("user", "a")])
    tools, servers = build_tools_declared([("Read", None, None)])
    declared.input.tools_declared, declared.input.tool_servers = tools, servers
    one = await build_event_from_normalized(bare, _plain_envelope(), config=_config())
    two = await build_event_from_normalized(declared, _plain_envelope(), config=_config())
    assert one.input == two.input


async def test_used_tools_still_resolve_against_history_in_round_scope() -> None:
    # the tool_use is one round back; its result is in the consumed round.
    history = [
        _msg("user", "go"),
        NormalizedMessage(
            role="assistant",
            content=[
                NormalizedContent(
                    kind="tool_use", tool_use_id="t1", tool_name="Read", tool_input={}
                )
            ],
        ),
        NormalizedMessage(
            role="user",
            content=[NormalizedContent(kind="tool_result", tool_use_id="t1", tool_output="x")],
        ),
    ]
    invocation = _invocation(history)
    tools, servers = build_tools_declared([("Read", None, None)])
    invocation.input.tools_declared, invocation.input.tool_servers = tools, servers
    event = await build_event_from_normalized(invocation, _plain_envelope(), config=_config())
    assert event.used_tools is not None and len(event.used_tools) == 1


async def test_event_carries_round_hash_and_recent_hashes() -> None:
    event = await build_event_from_normalized(
        _invocation([_msg("user", "a")]), _plain_envelope(), config=_config()
    )
    assert event.round_hash is not None
    assert event.recent_round_hashes == [event.round_hash, "start"]


async def test_round_link_depth_comes_from_config() -> None:
    history = [m for i in range(5) for m in (_msg("user", f"u{i}"), _msg("assistant", f"a{i}"))]
    event = await build_event_from_normalized(
        _invocation([*history, _msg("user", "u5")]),
        _plain_envelope(),
        config=_config(round_link_depth=3),
    )
    assert event.recent_round_hashes is not None
    assert len(event.recent_round_hashes) == 4 and event.recent_round_hashes[-1] == "..."


async def test_an_event_with_no_transcript_carries_no_links() -> None:
    event = await build_event_from_normalized(
        NormalizedInvocation(), _plain_envelope(), config=_config()
    )
    assert event.round_hash is None and event.recent_round_hashes is None


async def test_an_event_without_an_answer_has_no_round_hash() -> None:
    invocation = NormalizedInvocation(
        input=NormalizedInvocationInput(
            messages=[_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]
        )
    )
    event = await build_event_from_normalized(invocation, _plain_envelope(), config=_config())
    assert event.round_hash is None
    assert event.recent_round_hashes is not None and event.recent_round_hashes[-1] == "start"


def test_openai_identity_details_round_trips_through_the_union() -> None:
    from pydantic import ValidationError

    from slashid_ai_forwarder_core.events import OpenAIIdentityDetails

    details = OpenAIIdentityDetails(user_id="user-abc")
    assert details.model_dump(exclude_none=True) == {"kind": "openai", "user_id": "user-abc"}
    with pytest.raises(ValidationError, match="openai identity carries no identifier"):
        OpenAIIdentityDetails()
    env = EventEnvelope(
        request_id="r",
        timestamp="2026-10-01T00:00:00Z",
        identity_details=details,
        model=AIModel(id="gpt-5-codex"),
        parsed_as="openai-responses",
    )
    assert isinstance(env.identity_details, OpenAIIdentityDetails)
    event = AIInvocationObservedV1.model_validate(
        {
            "request_id": "r",
            "timestamp": "2026-10-01T00:00:00Z",
            "identity_details": {"kind": "openai", "user_id": "user-abc"},
            "model": {"id": "gpt-5-codex"},
            "parsed_as": "openai-responses",
        }
    )
    assert isinstance(event.identity_details, OpenAIIdentityDetails)


def test_tool_use_is_error_is_optional() -> None:
    from slashid_ai_forwarder_core.events import AIToolUse

    assert AIToolUse(tool_id="t").model_dump(exclude_none=True) == {"tool_id": "t"}


def _tool_call_invocation(*blocks: NormalizedContent) -> NormalizedInvocation:
    tools, servers = build_tools_declared([("Bash", None, None)])
    return NormalizedInvocation(
        input=NormalizedInvocationInput(
            messages=[_msg("user", "list files")], tools_declared=tools, tool_servers=servers
        ),
        output=NormalizedInvocationOutput(
            message=NormalizedMessage(role="assistant", content=list(blocks)),
            stop_reason="tool_use",
        ),
    )


def _bash_call(tool_use_id: str, name: str = "Bash", **extra: Any) -> NormalizedContent:
    return NormalizedContent(
        kind="tool_use",
        tool_use_id=tool_use_id,
        tool_name=name,
        tool_input={"command": "ls"},
        **extra,
    )


async def test_requested_tool_uses_lists_output_tool_calls() -> None:
    from slashid_ai_forwarder_core.events import AIToolUse

    invocation = _tool_call_invocation(_bash_call("call_1"), _bash_call("call_2", "Undeclared"))
    event = await build_event_from_normalized(invocation, _plain_envelope(), config=_config())
    bash_id = invocation.input.tools_declared[0].id
    assert event.requested_tool_uses == [AIToolUse(tool_id=bash_id, tool_use_id="call_1")]
    payload = event.model_dump(mode="json", exclude_none=True)
    assert payload["requested_tool_uses"] == [{"tool_id": bash_id, "tool_use_id": "call_1"}]


async def test_requested_tool_uses_absent_without_output_tool_calls() -> None:
    event = await build_event_from_normalized(
        _invocation([_msg("user", "a")]), _plain_envelope(), config=_config()
    )
    assert event.requested_tool_uses is None


async def test_requested_tool_uses_skips_server_executed_calls() -> None:
    invocation = _tool_call_invocation(_bash_call("call_1", tool_executor="server"))
    event = await build_event_from_normalized(invocation, _plain_envelope(), config=_config())
    assert event.requested_tool_uses is None


async def test_history_truncated_ends_round_links_in_the_marker() -> None:
    envelope = _plain_envelope().model_copy(update={"history_truncated": True})
    event = await build_event_from_normalized(
        _invocation([_msg("user", "a")]), envelope, config=_config()
    )
    assert event.recent_round_hashes is not None
    assert event.recent_round_hashes[-1] == "..."


async def test_used_tools_still_carry_is_error() -> None:
    record = _anthropic_record_with_bash(
        input_messages=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "x"}],
            },
        ],
    )
    event = await build_event_from_normalized(
        await anthropic_dict_to_normalized(record), _envelope(record), config=_config()
    )
    assert event.used_tools is not None
    assert event.used_tools[0].model_dump(exclude_none=True)["is_error"] is False


async def test_requested_tool_uses_from_anthropic_and_converse_normalizers() -> None:
    anthropic_record = _anthropic_record_with_bash(
        input_messages=[{"role": "user", "content": [{"type": "text", "text": "ls"}]}],
        output_content=[{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}],
    )
    converse_record = _record_with_bash(
        input_messages=[{"role": "user", "content": [{"text": "ls"}]}],
        output_content=[{"toolUse": {"toolUseId": "tooluse_1", "name": "Bash", "input": {}}}],
    )
    for normalized, record, use_id in (
        (await anthropic_dict_to_normalized(anthropic_record), anthropic_record, "toolu_1"),
        (
            await converse_dict_to_normalized(converse_record, config=_config()),
            converse_record,
            "tooluse_1",
        ),
    ):
        event = await build_event_from_normalized(normalized, _envelope(record), config=_config())
        declared = {t.id for t in normalized.input.tools_declared}
        assert event.requested_tool_uses is not None
        assert [(u.tool_id in declared, u.tool_use_id) for u in event.requested_tool_uses] == [
            (True, use_id)
        ]


async def test_requested_tool_uses_from_gemini_fixtures() -> None:
    from pathlib import Path

    import yaml

    fixtures = Path(__file__).parent / "normalize" / "test_gemini_to_normalized_invocation.yaml"
    cases = {
        doc["id"]: NormalizedInvocation.model_validate(doc["expected"])
        for doc in yaml.safe_load_all(fixtures.read_text())
        if doc
    }
    client = cases["response_function_call_synthesizes_id_from_output_turn_index"]
    event = await build_event_from_normalized(client, _plain_envelope(), config=_config())
    assert event.requested_tool_uses is not None
    assert [(u.tool_id, u.tool_use_id) for u in event.requested_tool_uses] == [
        (client.input.tools_declared[0].id, "gemini-11dace3b8c11589f")
    ]
    server = cases["server_side_executable_code_marks_server"]
    event = await build_event_from_normalized(server, _plain_envelope(), config=_config())
    assert event.requested_tool_uses is None
