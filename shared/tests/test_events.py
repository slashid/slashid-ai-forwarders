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
from typing import Any

import pytest

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIInvocationTokens,
    AIModel,
    AWSIdentityDetails,
    EventEnvelope,
    GCPIdentityDetails,
    _strip_empty_top,
    build_event_from_normalized,
    parse_tool_name,
)
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    anthropic_dict_to_normalized,
)
from slashid_ai_forwarder_core.normalize.converse.normalize import (
    converse_dict_to_normalized,
)


def _config(*, include_raw_content: bool = False, max_content_size: int = 100_000) -> BaseConfig:
    return BaseConfig(
        endpoint="http://test",
        push_token="test",
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
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
        "output": {"outputTokenCount": 50, "outputBodyJson": {"stopReason": "end_turn"}},
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

    obody = out.get("outputBodyJson")
    stop_raw = obody.get("stopReason") if isinstance(obody, dict) else None
    stop_reason = stop_raw if stop_raw in {"end_turn", "tool_use"} else None

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
        stop_reason=stop_reason,
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
        "parsed_as": "vertex-gemini-generate",
    }
    event = AIInvocationObservedV1.model_validate(raw_gcp)
    assert isinstance(event.identity_details, GCPIdentityDetails)
    assert event.identity_details.principal_email is None

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
        parsed_as="vertex-gemini-generate",
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

    # Hash the canonical input serialization — same as build_event_from_normalized does
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
    normalized = await converse_dict_to_normalized(record, config=_config())
    event = await build_event_from_normalized(
        normalized, _envelope(record), config=_config(include_raw_content=True)
    )
    assert event.input is not None
    assert event.input.redacted_text is not None
    # Canonical serialization is deterministic; compare via re-serialization
    # (matches build_event_from_normalized: empty top-level containers stripped
    # for hash stability).
    canonical = json.dumps(
        _strip_empty_top(normalized.input.model_dump(mode="json", exclude_none=True)),
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
