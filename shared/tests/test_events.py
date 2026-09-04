"""Unit tests for the pure transformation logic in `events`."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    _strip_empty_top,
    build_event,
    parse_tool_name,
)
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    anthropic_dict_to_normalized,
)
from slashid_ai_forwarder_core.normalize.converse.normalize import (
    converse_dict_to_normalized,
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
        "output": {"outputTokenCount": 50, "outputBodyJson": {"stopReason": "end_turn"}},
    }
    base.update(overrides)
    return base


async def test_build_event_minimal() -> None:
    record = _mil_record()
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert isinstance(event, AIInvocationObservedV1)
    assert event.request_id == "req-1"
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


async def test_build_event_skips_records_without_request_id() -> None:
    record = _mil_record()
    del record["requestId"]
    assert await build_event(converse_dict_to_normalized(record), record) is None


async def test_build_event_omits_access_key_when_missing() -> None:
    record = _mil_record(identity={"arn": "arn:aws:iam::123:user/bob"})
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.identity_details.principal_arn == "arn:aws:iam::123:user/bob"
    assert event.identity_details.access_key_id is None


async def test_build_event_skips_record_without_identity() -> None:
    """Regression for R1: a record with no usable principal ARN should drop,
    not ship as `identity_details.principal_arn = ""`."""
    record = _mil_record()
    record["identity"] = {}  # no arn, no resolved_arn
    assert await build_event(converse_dict_to_normalized(record), record) is None


async def test_build_event_skips_record_with_no_identity_block() -> None:
    record = _mil_record()
    del record["identity"]
    assert await build_event(converse_dict_to_normalized(record), record) is None


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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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

    Pair with ``converse_dict_to_normalized(record)``.
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

    Pair with ``anthropic_dict_to_normalized(record)``. Uses Anthropic
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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(anthropic_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(anthropic_dict_to_normalized(record), record)
    assert event is not None
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
    a_event = await build_event(anthropic_dict_to_normalized(anthropic_record), anthropic_record)
    c_event = await build_event(converse_dict_to_normalized(converse_record), converse_record)
    assert a_event is not None and a_event.used_tools is not None
    assert c_event is not None and c_event.used_tools is not None
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
    event = await build_event(anthropic_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(anthropic_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
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
    ev1 = await build_event(converse_dict_to_normalized(r1), r1)
    ev2 = await build_event(converse_dict_to_normalized(r2), r2)
    assert ev1 is not None and ev2 is not None
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
    ev1 = await build_event(converse_dict_to_normalized(r1), r1)
    ev2 = await build_event(converse_dict_to_normalized(r2), r2)
    assert ev1 is not None and ev2 is not None
    assert ev1.available_tools is not None and ev2.available_tools is not None
    assert ev1.available_tools[0].id != ev2.available_tools[0].id


async def test_build_event_populates_raw_model_id() -> None:
    # No region in base record → no catalog lookup → id falls back to raw
    record = _mil_record()
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.model.id == "us.anthropic.claude-sonnet-4-6"
    assert event.model.raw_model_id == "us.anthropic.claude-sonnet-4-6"
    assert event.model.name is None
    assert event.model.provider is None


async def test_build_event_uses_arn_as_id_when_raw_is_arn() -> None:
    arn = "arn:aws:bedrock:us-east-2:851725497009:inference-profile/us.anthropic.claude-sonnet-4-6"
    record = _mil_record(modelId=arn, region="us-east-2")
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    # Raw is already an ARN → used directly, no catalog needed
    assert event.model.id == arn
    assert event.model.raw_model_id == arn


async def test_build_event_enriches_model_from_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from slashid_ai_forwarder_core import model_catalog

    monkeypatch.setattr(
        model_catalog,
        "_catalogs",
        {
            "us-east-2": {
                "anthropic.claude-sonnet-4-6": model_catalog.ModelInfo(
                    arn="arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-6",
                    name="Claude Sonnet 4.6",
                    provider="Anthropic",
                )
            }
        },
    )
    record = _mil_record(modelId="us.anthropic.claude-sonnet-4-6", region="us-east-2")
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert (
        event.model.id == "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-6"
    )
    assert event.model.name == "Claude Sonnet 4.6"
    assert event.model.provider == "Anthropic"
    assert event.model.raw_model_id == "us.anthropic.claude-sonnet-4-6"


async def test_unknown_stop_reason_falls_back_to_unknown() -> None:
    record = _mil_record(output={"outputTokenCount": 5, "outputBodyJson": {"stopReason": "wat"}})
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.stop_reason == "unknown"


async def test_invalid_stop_reason_literal_rejected_on_construction() -> None:
    """Pydantic Literal type rejects values outside the AIStopReason enum."""
    from pydantic import ValidationError

    from slashid_ai_forwarder_core.events import AIInvocationObservedV1, AIModel, AWSIdentityDetails

    with pytest.raises(ValidationError):
        AIInvocationObservedV1(
            request_id="r",
            timestamp="t",
            identity_details=AWSIdentityDetails(principal_arn="arn:aws:iam::1:user/x"),
            model=AIModel(id="m"),
            parsed_as="anthropic-message",
            stop_reason="not-a-real-reason",  # ty: ignore[invalid-argument-type]
        )


async def test_content_fields_default_to_hash_only() -> None:
    """include_raw_content=False (default): hash + mime + bytes, no text."""
    body = {"messages": [{"role": "user", "content": [{"text": "secret prompt"}]}]}
    record = _mil_record(
        input={"inputTokenCount": 1, "inputBodyJson": body},
        output={"outputTokenCount": 1, "outputBodyJson": {"stopReason": "end_turn"}},
    )
    normalized = converse_dict_to_normalized(record)
    event = await build_event(normalized, record)
    assert event is not None
    assert event.input is not None

    # Hash the canonical input serialization — same as build_event does
    # (empty top-level containers stripped for pre-drive-by hash stability).
    canonical_input = json.dumps(
        _strip_empty_top(normalized.input.model_dump(mode="json", exclude_none=True)),
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
    normalized = converse_dict_to_normalized(record)
    event = await build_event(normalized, record, include_raw_content=True)
    assert event is not None
    assert event.input is not None
    assert event.input.redacted_text is not None
    # Canonical serialization is deterministic; compare via re-serialization
    # (matches build_event: empty top-level containers stripped for hash stability).
    canonical = json.dumps(
        _strip_empty_top(normalized.input.model_dump(mode="json", exclude_none=True)),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert event.input.redacted_text == canonical


async def test_content_field_none_when_body_absent() -> None:
    record = _mil_record(input={"inputTokenCount": 1}, output={"outputTokenCount": 1})
    normalized = converse_dict_to_normalized(record)
    event = await build_event(normalized, record)
    assert event is not None
    # Input side: normalized.input is empty → model_dump produces {} → _build_content returns None.
    assert event.input is None
    # Output side: normalized.output.stop_reason defaults to "unknown" → dumps to
    # {"stop_reason": "unknown"} → _build_content returns a hash of that.
    assert event.output is not None
    assert event.output.content_hashes is not None


def _record_with_messages(messages: list[Any]) -> dict[str, Any]:
    return _mil_record(
        input={
            "inputTokenCount": 10,
            "inputBodyJson": {
                "messages": messages,
            },
        },
        output={"outputTokenCount": 5, "outputBodyJson": {"stopReason": "end_turn"}},
    )


async def test_accessed_files_document_inline() -> None:
    content = b"hello world"
    b64 = base64.b64encode(content).decode()
    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {"document": {"name": "notes.txt", "format": "txt", "source": {"bytes": b64}}}
                ],
            }
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1
    f = event.accessed_files[0]
    assert f.name == "notes.txt"
    assert f.media_type == "text/plain"
    assert f.byte_length == len(content)
    assert f.content_hashes == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "sha1": hashlib.sha1(content).hexdigest(),
        "md5": hashlib.md5(content).hexdigest(),
    }
    assert f.redacted_content is None  # raw content opt-in off


async def test_accessed_files_document_raw_content_opt_in() -> None:
    content = b"secret data"
    b64 = base64.b64encode(content).decode()
    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {"document": {"name": "secret.txt", "format": "txt", "source": {"bytes": b64}}}
                ],
            }
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record, include_raw_content=True)
    assert event is not None
    assert event.accessed_files is not None
    assert event.accessed_files[0].redacted_content == "secret data"


async def test_accessed_files_image_inline() -> None:
    content = b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
    b64 = base64.b64encode(content).decode()
    record = _record_with_messages(
        [{"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": b64}}}]}]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1
    f = event.accessed_files[0]
    assert f.name is None  # images have no name
    assert f.media_type == "image/png"
    assert f.byte_length == len(content)
    assert f.content_hashes == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "sha1": hashlib.sha1(content).hexdigest(),
        "md5": hashlib.md5(content).hexdigest(),
    }


async def test_accessed_files_s3_source_uses_uri_as_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from slashid_ai_forwarder_core import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        pass  # no AWS calls — leave source without _resolved_* keys

    monkeypatch.setattr(s3_mod, "_resolve_s3_attachment", fake_resolve)

    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "name": "report.pdf",
                            "format": "pdf",
                            "source": {"s3Location": {"uri": "s3://my-bucket/report.pdf"}},
                        }
                    },
                    {
                        "image": {
                            "format": "jpeg",
                            "source": {"s3Location": {"uri": "s3://my-bucket/photo.jpg"}},
                        }
                    },
                ],
            }
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 2
    doc, img = event.accessed_files
    # document: name from doc.name, media_type from format, no bytes
    assert doc.name == "report.pdf"
    assert doc.media_type == "application/pdf"
    assert doc.content_hashes is None
    assert doc.byte_length is None
    # image: name from s3 URI, media_type from format
    assert img.name == "s3://my-bucket/photo.jpg"
    assert img.media_type == "image/jpeg"
    assert img.content_hashes is None


async def test_accessed_files_s3uri_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bedrock Playground sends source.s3Uri instead of source.s3Location.uri."""
    from slashid_ai_forwarder_core import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        source["_resolved_byte_length"] = 50000
        source["_resolved_content_type"] = "image/png"

    monkeypatch.setattr(s3_mod, "_resolve_s3_attachment", fake_resolve)

    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": "png",
                            "source": {"s3Uri": "s3://my-bucket/photo.png"},
                        }
                    }
                ],
            }
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1
    f = event.accessed_files[0]
    assert f.name == "s3://my-bucket/photo.png"
    assert f.media_type == "image/png"
    assert f.byte_length == 50000


async def test_accessed_files_s3_content_type_used_as_media_type_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Converse format is absent, ContentType from HeadObject is used as media_type."""
    from slashid_ai_forwarder_core import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        source["_resolved_byte_length"] = 100
        source["_resolved_content_type"] = "image/webp"

    monkeypatch.setattr(s3_mod, "_resolve_s3_attachment", fake_resolve)

    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            # No format field
                            "source": {"s3Location": {"uri": "s3://my-bucket/photo.webp"}},
                        }
                    }
                ],
            }
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert event.accessed_files[0].media_type == "image/webp"


@pytest.mark.parametrize(
    "fmt,expected_mime",
    [
        ("pdf", "application/pdf"),
        ("csv", "text/csv"),
        ("txt", "text/plain"),
        ("md", "text/markdown"),
        ("html", "text/html"),
        ("doc", "application/msword"),
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("xls", "application/vnd.ms-excel"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("png", "image/png"),
        ("jpeg", "image/jpeg"),
        ("gif", "image/gif"),
        ("webp", "image/webp"),
    ],
)
async def test_accessed_files_mime_map(fmt: str, expected_mime: str) -> None:
    """Every Bedrock format string maps to a correct IANA media type."""
    content = b"data"
    b64 = base64.b64encode(content).decode()
    key = "image" if fmt in ("png", "jpeg", "gif", "webp") else "document"
    block: dict[str, Any] = {
        key: {"format": fmt, "source": {"bytes": b64}},
    }
    if key == "document":
        block[key]["name"] = f"file.{fmt}"
    record = _record_with_messages([{"role": "user", "content": [block]}])
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert event.accessed_files[0].media_type == expected_mime


async def test_accessed_files_media_type_from_filename_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When format is absent and HeadObject returns no ContentType, guess from URI extension."""
    from slashid_ai_forwarder_core import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        source["_resolved_byte_length"] = 200
        # deliberately no _resolved_content_type

    monkeypatch.setattr(s3_mod, "_resolve_s3_attachment", fake_resolve)

    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {"image": {"source": {"s3Uri": "s3://bucket/photo.jpeg"}}},
                    {
                        "document": {
                            "name": "report",
                            "source": {"s3Uri": "s3://bucket/report.pdf"},
                        }
                    },
                ],
            }
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 2
    img, doc = event.accessed_files
    assert img.media_type == "image/jpeg"  # guessed from .jpeg in URI
    assert doc.media_type == "application/pdf"  # guessed from .pdf in URI


async def test_accessed_files_stub_has_media_type_from_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD-failed stub still gets media_type from the filename."""
    from slashid_ai_forwarder_core import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        pass  # HEAD failed — no _resolved_* keys

    monkeypatch.setattr(s3_mod, "_resolve_s3_attachment", fake_resolve)

    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {"image": {"source": {"s3Uri": "s3://bucket/photo.png"}}},
                ],
            }
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    f = event.accessed_files[0]
    assert f.name == "s3://bucket/photo.png"
    assert f.media_type == "image/png"
    assert f.byte_length is None
    assert f.content_hashes is None


async def test_accessed_files_non_dict_input_body_returns_empty() -> None:
    """Non-dict inputBodyJson (e.g. a list for non-Anthropic models) returns no files."""
    record = _mil_record(
        input={
            "inputTokenCount": 10,
            "inputBodyJson": [{"role": "user", "content": "text only"}],
        },
        output={"outputTokenCount": 5, "outputBodyJson": {"stopReason": "end_turn"}},
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is None


async def test_accessed_files_deduplicates_within_same_window() -> None:
    content = b"same file"
    b64 = base64.b64encode(content).decode()
    block = {"document": {"name": "dup.txt", "format": "txt", "source": {"bytes": b64}}}
    record = _record_with_messages(
        [
            {"role": "user", "content": [block]},
            {"role": "user", "content": [block]},  # same file repeated in same window
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1


async def test_accessed_files_only_from_last_user_turn() -> None:
    """Files in earlier turns (before the last assistant message) are ignored."""
    content_old = b"old file"
    content_new = b"new file"
    b64_old = base64.b64encode(content_old).decode()
    b64_new = base64.b64encode(content_new).decode()
    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {"document": {"name": "old.txt", "format": "txt", "source": {"bytes": b64_old}}}
                ],
            },
            {"role": "assistant", "content": [{"text": "ok"}]},
            {
                "role": "user",
                "content": [
                    {"document": {"name": "new.txt", "format": "txt", "source": {"bytes": b64_new}}}
                ],
            },
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    names = [f.name for f in event.accessed_files]
    assert "new.txt" in names
    assert "old.txt" not in names


async def test_accessed_files_all_included_when_no_prior_assistant_turn() -> None:
    """With no assistant message yet (first turn), all user files are included."""
    content = b"first turn file"
    b64 = base64.b64encode(content).decode()
    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {"document": {"name": "first.txt", "format": "txt", "source": {"bytes": b64}}}
                ],
            }
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert event.accessed_files[0].name == "first.txt"


async def test_accessed_files_none_when_no_attachments() -> None:
    record = _record_with_messages([{"role": "user", "content": [{"text": "just a text message"}]}])
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is None


def _record_with_tool_call(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result_content: str,
    prior_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a MIL record with one tool_use/tool_result pair in the last round."""
    tool_use_id = "tu_001"
    messages: list[dict[str, Any]] = list(prior_messages or [])
    messages += [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": tool_result_content,
                }
            ],
        },
    ]
    return _record_with_messages(messages)


async def test_accessed_files_tool_result_read() -> None:
    """Claude Code's Read tool returns cat-n formatted content; hash matches raw file bytes."""
    raw_content = "line1\nline2\n"
    cat_n_content = "     1\tline1\n     2\tline2\n"
    record = _record_with_tool_call(
        tool_name="Read",
        tool_input={"file_path": "/repo/src/main.py"},
        tool_result_content=cat_n_content,
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1
    f = event.accessed_files[0]
    assert f.name == "/repo/src/main.py"
    assert f.media_type == "text/x-python"
    assert f.byte_length == len(raw_content.encode())
    assert f.content_hashes == {
        "sha256": hashlib.sha256(raw_content.encode()).hexdigest(),
        "sha1": hashlib.sha1(raw_content.encode()).hexdigest(),
        "md5": hashlib.md5(raw_content.encode()).hexdigest(),
    }
    assert f.redacted_content is None  # raw content opt-in off


async def test_accessed_files_tool_result_read_no_prefix_falls_back() -> None:
    """If Read content lacks cat-n prefixes on any line, hash the content as-is."""
    content = "line1\nline2\n"  # no line-number prefixes
    record = _record_with_tool_call(
        tool_name="Read",
        tool_input={"file_path": "/repo/src/main.py"},
        tool_result_content=content,
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    f = event.accessed_files[0]
    assert f.content_hashes == {
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "sha1": hashlib.sha1(content.encode()).hexdigest(),
        "md5": hashlib.md5(content.encode()).hexdigest(),
    }
    assert f.byte_length == len(content.encode())


async def test_accessed_files_tool_result_raw_content_opt_in() -> None:
    content = "secret source"
    record = _record_with_tool_call(
        tool_name="Read",
        tool_input={"file_path": "/repo/secret.py"},
        tool_result_content=content,
    )
    event = await build_event(converse_dict_to_normalized(record), record, include_raw_content=True)
    assert event is not None
    assert event.accessed_files is not None
    assert event.accessed_files[0].redacted_content == content


async def test_accessed_files_tool_result_readfile_variant() -> None:
    """ReadFile (OpenCode/Q Developer) with 'path' input key is also recognised."""
    content = "data"
    record = _record_with_tool_call(
        tool_name="ReadFile",
        tool_input={"path": "/repo/config.json"},
        tool_result_content=content,
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    f = event.accessed_files[0]
    assert f.name == "/repo/config.json"
    assert f.media_type == "application/json"
    assert f.byte_length == len(content.encode())


async def test_accessed_files_unknown_tool_ignored() -> None:
    """Non-read tools (Bash, WebFetch, etc.) do not produce accessed_files entries."""
    record = _record_with_tool_call(
        tool_name="Bash",
        tool_input={"command": "ls -la"},
        tool_result_content="total 8\n...",
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is None


async def test_accessed_files_tool_result_only_last_turn() -> None:
    """Tool results from before the last assistant message are ignored."""
    content_old = "old file content"
    content_new = "new file content"
    tool_use_id_old = "tu_old"
    tool_use_id_new = "tu_new"
    record = _record_with_messages(
        [
            # First round — should be ignored
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id_old,
                        "name": "Read",
                        "input": {"file_path": "/old.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id_old,
                        "content": content_old,
                    }
                ],
            },
            # Second round — should be captured
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id_new,
                        "name": "Read",
                        "input": {"file_path": "/new.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id_new,
                        "content": content_new,
                    }
                ],
            },
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1
    assert event.accessed_files[0].name == "/new.py"


async def test_accessed_files_dedup_tool_and_attachment() -> None:
    """Same file from both a document block and a Read tool result is deduplicated."""
    content = b"shared content"
    b64 = base64.b64encode(content).decode()
    tool_use_id = "tu_dup"
    record = _record_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "name": "shared.txt",
                            "format": "txt",
                            "source": {"bytes": b64},
                        }
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "Read",
                        "input": {"file_path": "shared.txt"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content.decode(),
                    }
                ],
            },
        ]
    )
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1  # deduped by (name, content_hash)


async def test_build_event_wire_form() -> None:
    record = _mil_record()
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    wire = event.model_dump(mode="json", exclude_none=True)
    # Spec dropped these; they must not show up in the JSON we send.
    assert "org_id" not in wire
    assert "connection_id" not in wire
    assert "identifier_from_source" not in wire
    assert "identity_source_type" not in wire
    # The new identity shape.
    assert wire["identity_details"] == {
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
    event = await build_event(
        converse_dict_to_normalized(record),
        record,
        include_raw_content=True,
        max_content_size=20,
    )
    assert event is not None
    assert event.input is not None
    assert event.input.redacted_text is not None
    assert len(event.input.redacted_text) <= 20
    assert "…" in event.input.redacted_text


@pytest.mark.asyncio
async def test_build_event_truncates_file_redacted_content() -> None:
    """redacted_content on accessed files is capped to max_content_size."""
    raw = "line1\nline2\nline3\n" * 50  # 900 chars, 150 lines
    cat_n = "".join(f"     {i + 1}\t{line}\n" for i, line in enumerate(raw.splitlines()))
    record = _record_with_tool_call(
        tool_name="Read",
        tool_input={"file_path": "/repo/big.txt"},
        tool_result_content=cat_n,
    )
    event = await build_event(
        converse_dict_to_normalized(record),
        record,
        include_raw_content=True,
        max_content_size=50,
    )
    assert event is not None
    assert event.accessed_files is not None
    f = event.accessed_files[0]
    assert f.redacted_content is not None
    assert len(f.redacted_content) <= 50
    assert "…" in f.redacted_content
    # hash and byte_length reflect the full stripped content, not the truncated string
    assert f.byte_length == len(raw.encode())


async def test_build_event_populates_parsed_as_from_record() -> None:
    # normalize_record sets record["_parsed_as"]; build_event surfaces
    # it on the wire model.
    record = _mil_record()
    record["_parsed_as"] = "anthropic-message"
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.parsed_as == "anthropic-message"


async def test_build_event_parsed_as_defaults_to_unknown() -> None:
    # If _parsed_as is missing (bypass path — defensive; not exercised in
    # normal handler flow), build_event falls back to "unknown".
    record = _mil_record()  # no _parsed_as set
    event = await build_event(converse_dict_to_normalized(record), record)
    assert event is not None
    assert event.parsed_as == "unknown"
