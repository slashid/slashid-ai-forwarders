"""Unit tests for the pure transformation logic in `events`."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from slashid_bedrock_forwarder.events import (
    AIInvocationObservedV1,
    build_event,
    parse_tool_name,
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
    event = await build_event(_mil_record())
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
    assert event.used_tool_ids is None
    assert event.available_agents is None
    assert event.used_agent_ids is None


async def test_build_event_skips_records_without_request_id() -> None:
    record = _mil_record()
    del record["requestId"]
    assert await build_event(record) is None


async def test_build_event_omits_access_key_when_missing() -> None:
    record = _mil_record(identity={"arn": "arn:aws:iam::123:user/bob"})
    event = await build_event(record)
    assert event is not None
    assert event.identity_details.principal_arn == "arn:aws:iam::123:user/bob"
    assert event.identity_details.access_key_id is None


async def test_build_event_skips_record_without_identity() -> None:
    """Regression for R1: a record with no usable principal ARN should drop,
    not ship as `identity_details.principal_arn = ""`."""
    record = _mil_record()
    record["identity"] = {}  # no arn, no resolved_arn
    assert await build_event(record) is None


async def test_build_event_skips_record_with_no_identity_block() -> None:
    record = _mil_record()
    del record["identity"]
    assert await build_event(record) is None


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
                }
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
    event = await build_event(record)
    assert event is not None
    assert event.available_tool_servers is not None
    assert event.available_tools is not None
    assert event.used_tool_ids is not None

    servers = {s.name: s for s in event.available_tool_servers}
    assert servers["excalidraw"].kind == "mcp"
    assert servers["builtin"].kind == "runtime"

    tools_by_name = {t.name: t for t in event.available_tools}
    assert tools_by_name["create_view"].tool_server_id == servers["excalidraw"].id
    assert tools_by_name["Bash"].tool_server_id == servers["builtin"].id

    assert event.used_tool_ids == [tools_by_name["create_view"].id]
    assert event.stop_reason == "tool_use"


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
                    }
                },
            }
        )

    ev1 = await build_event(
        _record_with_schema({"type": "object", "properties": {"url": {"type": "string"}}})
    )
    ev2 = await build_event(
        _record_with_schema(
            {
                "type": "object",
                "properties": {"url": {"type": "string"}, "depth": {"type": "integer"}},
            }
        )
    )
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
                    "toolConfig": {"tools": [{"toolSpec": {"name": "Bash", "description": desc}}]}
                },
            }
        )

    ev1 = await build_event(_record_with_desc("Run a shell command"))
    ev2 = await build_event(
        _record_with_desc("Execute arbitrary shell commands with elevated privileges")
    )
    assert ev1 is not None and ev2 is not None
    assert ev1.available_tools is not None and ev2.available_tools is not None
    assert ev1.available_tools[0].id != ev2.available_tools[0].id


async def test_build_event_populates_raw_model_id() -> None:
    # No region in base record → no catalog lookup → id falls back to raw
    event = await build_event(_mil_record())
    assert event is not None
    assert event.model.id == "us.anthropic.claude-sonnet-4-6"
    assert event.model.raw_model_id == "us.anthropic.claude-sonnet-4-6"
    assert event.model.name is None
    assert event.model.provider is None


async def test_build_event_uses_arn_as_id_when_raw_is_arn() -> None:
    arn = "arn:aws:bedrock:us-east-2:851725497009:inference-profile/us.anthropic.claude-sonnet-4-6"
    record = _mil_record(modelId=arn, region="us-east-2")
    event = await build_event(record)
    assert event is not None
    # Raw is already an ARN → used directly, no catalog needed
    assert event.model.id == arn
    assert event.model.raw_model_id == arn


async def test_build_event_enriches_model_from_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from slashid_bedrock_forwarder import model_catalog

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
    event = await build_event(record)
    assert event is not None
    assert (
        event.model.id == "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-6"
    )
    assert event.model.name == "Claude Sonnet 4.6"
    assert event.model.provider == "Anthropic"
    assert event.model.raw_model_id == "us.anthropic.claude-sonnet-4-6"


async def test_unknown_stop_reason_falls_back_to_unknown() -> None:
    event = await build_event(
        _mil_record(output={"outputTokenCount": 5, "outputBodyJson": {"stopReason": "wat"}}),
    )
    assert event is not None
    assert event.stop_reason == "unknown"


async def test_invalid_stop_reason_literal_rejected_on_construction() -> None:
    """Pydantic Literal type rejects values outside the AIStopReason enum."""
    from pydantic import ValidationError

    from slashid_bedrock_forwarder.events import AIInvocationObservedV1, AIModel, AWSIdentityDetails

    with pytest.raises(ValidationError):
        AIInvocationObservedV1(
            request_id="r",
            timestamp="t",
            identity_details=AWSIdentityDetails(principal_arn="arn:aws:iam::1:user/x"),
            model=AIModel(id="m"),
            stop_reason="not-a-real-reason",  # ty: ignore[invalid-argument-type]
        )


async def test_content_fields_default_to_hash_only() -> None:
    """include_raw_content=False (default): hash + mime + bytes, no text."""
    body = {"messages": [{"role": "user", "content": "secret prompt"}]}
    record = _mil_record(
        input={"inputTokenCount": 1, "inputBodyJson": body},
        output={"outputTokenCount": 1, "outputBodyJson": {"stopReason": "end_turn"}},
    )
    event = await build_event(record)
    assert event is not None
    assert event.input is not None

    serialised = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    expected_hash = f"sha256:{hashlib.sha256(serialised).hexdigest()}"
    assert event.input.content_hash == expected_hash
    assert event.input.mime_type == "application/json"
    assert event.input.byte_length == len(serialised)
    # Crucially: no text.
    assert event.input.redacted_text is None


async def test_content_fields_include_raw_when_opted_in() -> None:
    body = {"messages": [{"role": "user", "content": "hello"}]}
    record = _mil_record(
        input={"inputTokenCount": 1, "inputBodyJson": body},
        output={"outputTokenCount": 1, "outputBodyJson": {"stopReason": "end_turn"}},
    )
    event = await build_event(record, include_raw_content=True)
    assert event is not None
    assert event.input is not None
    assert event.input.redacted_text is not None
    assert json.loads(event.input.redacted_text) == body


async def test_content_field_none_when_body_absent() -> None:
    record = _mil_record(input={"inputTokenCount": 1}, output={"outputTokenCount": 1})
    event = await build_event(record)
    assert event is not None
    assert event.input is None
    assert event.output is None


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
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1
    f = event.accessed_files[0]
    assert f.name == "notes.txt"
    assert f.mime_type == "application/txt"
    assert f.byte_length == len(content)
    assert f.content_hash == f"sha256:{hashlib.sha256(content).hexdigest()}"
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
    event = await build_event(record, include_raw_content=True)
    assert event is not None
    assert event.accessed_files is not None
    assert event.accessed_files[0].redacted_content == "secret data"


async def test_accessed_files_image_inline() -> None:
    content = b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
    b64 = base64.b64encode(content).decode()
    record = _record_with_messages(
        [{"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": b64}}}]}]
    )
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1
    f = event.accessed_files[0]
    assert f.name is None  # images have no name
    assert f.mime_type == "image/png"
    assert f.byte_length == len(content)
    assert f.content_hash == f"sha256:{hashlib.sha256(content).hexdigest()}"


async def test_accessed_files_s3_source_uses_uri_as_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from slashid_bedrock_forwarder import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
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
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 2
    doc, img = event.accessed_files
    # document: name from the doc.name field (s3 hint is fallback)
    assert doc.name == "report.pdf"
    assert doc.content_hash is None  # no bytes available
    assert doc.byte_length is None
    # image: name from s3 URI (images have no name field)
    assert img.name == "s3://my-bucket/photo.jpg"
    assert img.content_hash is None


async def test_accessed_files_deduplicates_across_turns() -> None:
    content = b"same file"
    b64 = base64.b64encode(content).decode()
    block = {"document": {"name": "dup.txt", "format": "txt", "source": {"bytes": b64}}}
    record = _record_with_messages(
        [
            {"role": "user", "content": [block]},
            {"role": "user", "content": [block]},  # same file in second turn
        ]
    )
    event = await build_event(record)
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
    event = await build_event(record)
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
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    assert event.accessed_files[0].name == "first.txt"


async def test_accessed_files_none_when_no_attachments() -> None:
    record = _record_with_messages([{"role": "user", "content": [{"text": "just a text message"}]}])
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is None


async def test_build_event_wire_form() -> None:
    event = await build_event(_mil_record())
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
    assert "used_tool_ids" not in wire
    assert "available_agents" not in wire
    # Tokens always present (default 0s).
    assert wire["tokens"] == {
        "input": 100,
        "output": 50,
        "cache_read": 5,
        "cache_write": 0,
        "reasoning": 0,
    }
