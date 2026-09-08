"""Tests for Converse document/image attachment extraction.

Migrated from ``bedrock/tests/test_converse_attachments.py`` when the
extraction moved into the shared ``converse/attachments.py`` walking typed
pydantic (``ConverseRequestBody``) instead of raw dicts. Exercises
``extract_attachments`` via the public ``to_normalized_invocation`` entry
point so both halves stay covered by real dispatch.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.converse import attachments as converse_attachments
from slashid_ai_forwarder_core.normalize.converse.normalize import to_normalized_invocation
from slashid_ai_forwarder_core.normalize.converse.schema import (
    ConverseRequestBody,
    ConverseResponse,
)
from slashid_ai_forwarder_core.normalize.finalize import finalize


def _config(*, include_raw_content: bool = False, max_content_size: int = 100_000) -> BaseConfig:
    return BaseConfig(
        endpoint="http://test",
        push_token="test",
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )


_EMPTY_RESPONSE = ConverseResponse.model_validate(
    {"output": {"message": {"role": "assistant", "content": []}}, "stopReason": "end_turn"}
)


async def _extract(
    messages: list[dict[str, Any]] | None,
    *,
    include_raw_content: bool = False,
    max_content_size: int = 100_000,
) -> list[AIAccessedFile]:
    """Validate a raw messages list into ConverseRequestBody, dispatch through
    to_normalized_invocation + finalize (mirrors the real handler pipeline
    so tests observe the canonical, deduped accessed_files list)."""
    body_dict: dict[str, Any] = {"messages": messages if messages is not None else []}
    request = ConverseRequestBody.model_validate(body_dict)
    cfg = _config(include_raw_content=include_raw_content, max_content_size=max_content_size)
    normalized = await to_normalized_invocation(request, _EMPTY_RESPONSE, config=cfg)
    finalize(normalized, config=cfg)
    return normalized.accessed_files


async def test_document_inline() -> None:
    content = b"hello world"
    b64 = base64.b64encode(content).decode()
    files = await _extract(
        [
            {
                "role": "user",
                "content": [
                    {"document": {"name": "notes.txt", "format": "txt", "source": {"bytes": b64}}}
                ],
            }
        ]
    )
    assert len(files) == 1
    f = files[0]
    assert f.name == "notes.txt"
    assert f.media_type == "text/plain"
    assert f.byte_length == len(content)
    assert f.content_hashes == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "sha1": hashlib.sha1(content).hexdigest(),
        "md5": hashlib.md5(content).hexdigest(),
    }
    assert f.redacted_content is None  # raw content opt-in off


async def test_document_raw_content_opt_in() -> None:
    content = b"secret data"
    b64 = base64.b64encode(content).decode()
    files = await _extract(
        [
            {
                "role": "user",
                "content": [
                    {"document": {"name": "secret.txt", "format": "txt", "source": {"bytes": b64}}}
                ],
            }
        ],
        include_raw_content=True,
    )
    assert len(files) == 1
    assert files[0].redacted_content == "secret data"


async def test_image_inline() -> None:
    content = b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
    b64 = base64.b64encode(content).decode()
    files = await _extract(
        [{"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": b64}}}]}]
    )
    assert len(files) == 1
    f = files[0]
    assert f.name is None  # images have no name
    assert f.media_type == "image/png"
    assert f.byte_length == len(content)
    assert f.content_hashes == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "sha1": hashlib.sha1(content).hexdigest(),
        "md5": hashlib.md5(content).hexdigest(),
    }


async def test_s3_source_uses_uri_as_name(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        pass  # no AWS calls — leave source without _resolved_* keys

    monkeypatch.setattr(converse_attachments, "_resolve_s3_attachment", fake_resolve)

    files = await _extract(
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
    assert len(files) == 2
    doc, img = files
    # document: name from doc.name, media_type from format, no bytes
    assert doc.name == "report.pdf"
    assert doc.media_type == "application/pdf"
    assert doc.content_hashes is None
    assert doc.byte_length is None
    # image: name from s3 URI, media_type from format
    assert img.name == "s3://my-bucket/photo.jpg"
    assert img.media_type == "image/jpeg"
    assert img.content_hashes is None


async def test_s3uri_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bedrock Playground sends source.s3Uri instead of source.s3Location.uri."""

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        source["_resolved_byte_length"] = 50000
        source["_resolved_content_type"] = "image/png"

    monkeypatch.setattr(converse_attachments, "_resolve_s3_attachment", fake_resolve)

    files = await _extract(
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
    assert len(files) == 1
    f = files[0]
    assert f.name == "s3://my-bucket/photo.png"
    assert f.media_type == "image/png"
    assert f.byte_length == 50000


async def test_s3_content_type_used_as_media_type_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Converse format is absent, ContentType from HeadObject is used as media_type."""

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        source["_resolved_byte_length"] = 100
        source["_resolved_content_type"] = "image/webp"

    monkeypatch.setattr(converse_attachments, "_resolve_s3_attachment", fake_resolve)

    files = await _extract(
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
    assert len(files) == 1
    assert files[0].media_type == "image/webp"


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
async def test_mime_map(fmt: str, expected_mime: str) -> None:
    """Every Bedrock format string maps to a correct IANA media type."""
    content = b"data"
    b64 = base64.b64encode(content).decode()
    key = "image" if fmt in ("png", "jpeg", "gif", "webp") else "document"
    block: dict[str, Any] = {
        key: {"format": fmt, "source": {"bytes": b64}},
    }
    if key == "document":
        block[key]["name"] = f"file.{fmt}"
    files = await _extract([{"role": "user", "content": [block]}])
    assert len(files) == 1
    assert files[0].media_type == expected_mime


async def test_media_type_from_filename_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When format is absent and HeadObject returns no ContentType, guess from URI extension."""

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        source["_resolved_byte_length"] = 200
        # deliberately no _resolved_content_type

    monkeypatch.setattr(converse_attachments, "_resolve_s3_attachment", fake_resolve)

    files = await _extract(
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
    assert len(files) == 2
    img, doc = files
    assert img.media_type == "image/jpeg"  # guessed from .jpeg in URI
    assert doc.media_type == "application/pdf"  # guessed from .pdf in URI


async def test_stub_has_media_type_from_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD-failed stub still gets media_type from the filename."""

    async def fake_resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        pass  # HEAD failed — no _resolved_* keys

    monkeypatch.setattr(converse_attachments, "_resolve_s3_attachment", fake_resolve)

    files = await _extract(
        [
            {
                "role": "user",
                "content": [
                    {"image": {"source": {"s3Uri": "s3://bucket/photo.png"}}},
                ],
            }
        ]
    )
    assert len(files) == 1
    f = files[0]
    assert f.name == "s3://bucket/photo.png"
    assert f.media_type == "image/png"
    assert f.byte_length is None
    assert f.content_hashes is None


async def test_non_dict_input_body_returns_empty() -> None:
    """No messages → no files (mirrors the earlier non-dict inputBodyJson test)."""
    files = await _extract([])
    assert files == []


async def test_deduplicates_within_same_window() -> None:
    content = b"same file"
    b64 = base64.b64encode(content).decode()
    block = {"document": {"name": "dup.txt", "format": "txt", "source": {"bytes": b64}}}
    files = await _extract(
        [
            {"role": "user", "content": [block]},
            {"role": "user", "content": [block]},  # same file repeated in same window
        ]
    )
    assert len(files) == 1


async def test_only_from_last_user_turn() -> None:
    """Files in earlier turns (before the last assistant message) are ignored."""
    content_old = b"old file"
    content_new = b"new file"
    b64_old = base64.b64encode(content_old).decode()
    b64_new = base64.b64encode(content_new).decode()
    files = await _extract(
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
    names = [f.name for f in files]
    assert "new.txt" in names
    assert "old.txt" not in names


async def test_all_included_when_no_prior_assistant_turn() -> None:
    """With no assistant message yet (first turn), all user files are included."""
    content = b"first turn file"
    b64 = base64.b64encode(content).decode()
    files = await _extract(
        [
            {
                "role": "user",
                "content": [
                    {"document": {"name": "first.txt", "format": "txt", "source": {"bytes": b64}}}
                ],
            }
        ]
    )
    assert len(files) == 1
    assert files[0].name == "first.txt"


async def test_none_when_no_attachments() -> None:
    files = await _extract([{"role": "user", "content": [{"text": "just a text message"}]}])
    assert files == []
