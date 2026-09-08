"""Tests for Gemini inlineData / fileData attachment extraction.

Exercises ``extract_attachments`` via the public
``to_normalized_invocation`` entry point so both halves stay covered by
real dispatch.
"""

from __future__ import annotations

import base64
import hashlib

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.gemini.normalize import to_normalized_invocation
from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiRequestBody,
    GeminiResponse,
)

_EMPTY_RESPONSE = GeminiResponse.model_validate(
    {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "STOP"}]}
)


def _config(*, include_raw_content: bool = False, max_content_size: int = 100_000) -> BaseConfig:
    return BaseConfig(
        endpoint="http://test",
        push_token="test",
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )


async def _extract(
    contents: list[dict],  # type: ignore[type-arg]
    *,
    include_raw_content: bool = False,
    max_content_size: int = 100_000,
) -> list[AIAccessedFile]:
    request = GeminiRequestBody.model_validate({"contents": contents})
    normalized = await to_normalized_invocation(
        request,
        _EMPTY_RESPONSE,
        config=_config(
            include_raw_content=include_raw_content,
            max_content_size=max_content_size,
        ),
    )
    return normalized.accessed_files


async def test_inline_data_hashes_bytes() -> None:
    content = b"hello world"
    b64 = base64.b64encode(content).decode()
    files = await _extract(
        [
            {
                "role": "user",
                "parts": [{"inlineData": {"mimeType": "text/plain", "data": b64}}],
            }
        ]
    )
    assert len(files) == 1
    f = files[0]
    assert f.name is None
    assert f.media_type == "text/plain"
    assert f.byte_length == len(content)
    assert f.content_hashes == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "sha1": hashlib.sha1(content).hexdigest(),
        "md5": hashlib.md5(content).hexdigest(),
    }
    assert f.redacted_content is None


async def test_inline_data_include_raw_content_opt_in() -> None:
    content = b"secret data"
    b64 = base64.b64encode(content).decode()
    files = await _extract(
        [
            {
                "role": "user",
                "parts": [{"inlineData": {"mimeType": "text/plain", "data": b64}}],
            }
        ],
        include_raw_content=True,
    )
    assert len(files) == 1
    assert files[0].redacted_content == "secret data"


async def test_file_data_stubs_without_hash() -> None:
    files = await _extract(
        [
            {
                "role": "user",
                "parts": [
                    {
                        "fileData": {
                            "mimeType": "application/pdf",
                            "fileUri": "gs://my-bucket/notes.pdf",
                        }
                    }
                ],
            }
        ]
    )
    assert len(files) == 1
    f = files[0]
    assert f.name == "gs://my-bucket/notes.pdf"
    assert f.media_type == "application/pdf"
    assert f.byte_length is None
    assert f.content_hashes is None


async def test_file_data_media_type_falls_back_to_uri_extension() -> None:
    """When mimeType is absent, guess media type from the gs:// URI."""
    files = await _extract(
        [
            {
                "role": "user",
                "parts": [{"fileData": {"fileUri": "gs://my-bucket/photo.png"}}],
            }
        ]
    )
    assert len(files) == 1
    assert files[0].media_type == "image/png"


async def test_only_files_from_last_model_turn_forward() -> None:
    """Fresh-turn semantics: attachments before the last model turn are
    already reported on prior events — skip them."""
    old = b"old file"
    new = b"new file"
    b64_old = base64.b64encode(old).decode()
    b64_new = base64.b64encode(new).decode()
    files = await _extract(
        [
            {
                "role": "user",
                "parts": [{"inlineData": {"mimeType": "text/plain", "data": b64_old}}],
            },
            {"role": "model", "parts": [{"text": "ok"}]},
            {
                "role": "user",
                "parts": [{"inlineData": {"mimeType": "text/plain", "data": b64_new}}],
            },
        ]
    )
    assert len(files) == 1
    assert files[0].byte_length == len(new)


async def test_all_included_when_no_prior_model_turn() -> None:
    """With no model message yet (first turn), all user attachments are
    included — matches the Converse convention."""
    content = b"first turn file"
    files = await _extract(
        [
            {
                "role": "user",
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "text/plain",
                            "data": base64.b64encode(content).decode(),
                        }
                    }
                ],
            }
        ]
    )
    assert len(files) == 1
    assert files[0].byte_length == len(content)


async def test_deduplicates_by_name_and_sha256_within_same_window() -> None:
    content = b"same file"
    b64 = base64.b64encode(content).decode()
    files = await _extract(
        [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": "text/plain", "data": b64}},
                    {"inlineData": {"mimeType": "text/plain", "data": b64}},
                ],
            }
        ]
    )
    assert len(files) == 1


async def test_none_when_no_attachments() -> None:
    files = await _extract([{"role": "user", "parts": [{"text": "just a text message"}]}])
    assert files == []


async def test_corrupt_base64_emits_stub() -> None:
    """Malformed base64 shouldn't crash — emit a stub AIAccessedFile so
    the caller sees the reference."""
    files = await _extract(
        [
            {
                "role": "user",
                "parts": [{"inlineData": {"mimeType": "image/png", "data": "!!!not-base64!!!"}}],
            }
        ]
    )
    assert len(files) == 1
    assert files[0].content_hashes is None
    assert files[0].byte_length is None
    assert files[0].media_type == "image/png"
