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
    assert f.media_type == "text/plain"
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
    assert f.media_type == "image/png"
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
    # document: name from doc.name, media_type from format, no bytes
    assert doc.name == "report.pdf"
    assert doc.media_type == "application/pdf"
    assert doc.content_hash is None
    assert doc.byte_length is None
    # image: name from s3 URI, media_type from format
    assert img.name == "s3://my-bucket/photo.jpg"
    assert img.media_type == "image/jpeg"
    assert img.content_hash is None


async def test_accessed_files_s3uri_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bedrock Playground sends source.s3Uri instead of source.s3Location.uri."""
    from slashid_bedrock_forwarder import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
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
    event = await build_event(record)
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
    from slashid_bedrock_forwarder import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
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
    event = await build_event(record)
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
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    assert event.accessed_files[0].media_type == expected_mime


async def test_accessed_files_media_type_from_filename_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When format is absent and HeadObject returns no ContentType, guess from URI extension."""
    from slashid_bedrock_forwarder import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
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
    event = await build_event(record)
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
    from slashid_bedrock_forwarder import s3 as s3_mod

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
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
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    f = event.accessed_files[0]
    assert f.name == "s3://bucket/photo.png"
    assert f.media_type == "image/png"
    assert f.byte_length is None
    assert f.content_hash is None


async def test_accessed_files_non_dict_input_body_returns_empty() -> None:
    """Non-dict inputBodyJson (e.g. a list for non-Anthropic models) returns no files."""
    record = _mil_record(
        input={
            "inputTokenCount": 10,
            "inputBodyJson": [{"role": "user", "content": "text only"}],
        },
        output={"outputTokenCount": 5, "outputBodyJson": {"stopReason": "end_turn"}},
    )
    event = await build_event(record)
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
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1
    f = event.accessed_files[0]
    assert f.name == "/repo/src/main.py"
    assert f.media_type == "text/x-python"
    assert f.byte_length == len(raw_content.encode())
    assert f.content_hash == f"sha256:{hashlib.sha256(raw_content.encode()).hexdigest()}"
    assert f.redacted_content is None  # raw content opt-in off


async def test_accessed_files_tool_result_read_no_prefix_falls_back() -> None:
    """If Read content lacks cat-n prefixes on any line, hash the content as-is."""
    content = "line1\nline2\n"  # no line-number prefixes
    record = _record_with_tool_call(
        tool_name="Read",
        tool_input={"file_path": "/repo/src/main.py"},
        tool_result_content=content,
    )
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    f = event.accessed_files[0]
    assert f.content_hash == f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
    assert f.byte_length == len(content.encode())


async def test_accessed_files_tool_result_raw_content_opt_in() -> None:
    content = "secret source"
    record = _record_with_tool_call(
        tool_name="Read",
        tool_input={"file_path": "/repo/secret.py"},
        tool_result_content=content,
    )
    event = await build_event(record, include_raw_content=True)
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
    event = await build_event(record)
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
    event = await build_event(record)
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
    event = await build_event(record)
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
    event = await build_event(record)
    assert event is not None
    assert event.accessed_files is not None
    assert len(event.accessed_files) == 1  # deduped by (name, content_hash)


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
