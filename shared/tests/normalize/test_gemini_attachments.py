"""Tests for Gemini inlineData / fileData attachment extraction.

Exercises ``extract_attachments`` via the public
``to_normalized_invocation`` entry point so both halves stay covered by
real dispatch.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.gemini import attachments as _attachments
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
    """Run the full pipeline (extract → finalize) so tests observe the
    canonical, deduped accessed_files list — matches real handler use."""
    request = GeminiRequestBody.model_validate({"contents": contents})
    cfg = _config(include_raw_content=include_raw_content, max_content_size=max_content_size)
    normalized = await to_normalized_invocation(request, _EMPTY_RESPONSE, config=cfg)
    finalize(normalized, config=cfg)
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


async def test_file_data_resolved_metadata_only_populates_md5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default path: HEAD succeeds → wire entry has md5, byte_length, media_type
    from HEAD (fileData.mimeType wins over HEAD.contentType when both present)."""
    raw = b"hello world"

    async def _fake_resolve(source: dict, **_kw) -> None:  # type: ignore[type-arg]
        source["_resolved_byte_length"] = len(raw)
        source["_resolved_md5_hex"] = hashlib.md5(raw).hexdigest()
        source["_resolved_content_type"] = "text/plain"

    monkeypatch.setattr(_attachments, "_resolve_gcs_attachment", _fake_resolve)

    files = await _extract(
        [
            {
                "role": "user",
                "parts": [
                    {
                        "fileData": {
                            "mimeType": "application/pdf",  # explicit wins
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
    assert f.content_hashes == {"md5": hashlib.md5(raw).hexdigest()}
    assert f.byte_length == len(raw)
    assert f.media_type == "application/pdf"  # part.mimeType overrides HEAD.contentType
    assert f.redacted_content is None


async def test_file_data_media_type_falls_back_to_head_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fileData.mimeType is absent, HEAD.contentType wins over filename guess."""
    async def _fake_resolve(source: dict, **_kw) -> None:  # type: ignore[type-arg]
        source["_resolved_byte_length"] = 100
        source["_resolved_md5_hex"] = "deadbeef"
        source["_resolved_content_type"] = "application/x-custom"

    monkeypatch.setattr(_attachments, "_resolve_gcs_attachment", _fake_resolve)

    files = await _extract(
        [
            {
                "role": "user",
                "parts": [{"fileData": {"fileUri": "gs://my-bucket/photo.png"}}],
            }
        ]
    )
    assert len(files) == 1
    # HEAD contentType wins over the ``.png`` extension guess when part.mimeType is absent.
    assert files[0].media_type == "application/x-custom"


async def test_file_data_head_failure_emits_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When resolver leaves source untouched (HEAD 404/403) → stub matches Phase 3.1
    shape: URI as name, media_type from part or extension guess, everything else None."""
    async def _fake_resolve(source: dict, **_kw) -> None:  # type: ignore[type-arg]
        return  # simulate HEAD failure

    monkeypatch.setattr(_attachments, "_resolve_gcs_attachment", _fake_resolve)

    files = await _extract(
        [
            {
                "role": "user",
                "parts": [
                    {"fileData": {"mimeType": "application/pdf", "fileUri": "gs://x/y.pdf"}}
                ],
            }
        ]
    )
    assert len(files) == 1
    f = files[0]
    assert f.name == "gs://x/y.pdf"
    assert f.media_type == "application/pdf"
    assert f.byte_length is None
    assert f.content_hashes is None
    assert f.redacted_content is None


async def test_file_data_full_body_computes_all_three_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in + fits: full bytes stashed → sha256+sha1+md5 all present + redacted_content
    populated (no truncation — file fits under cap)."""
    raw = b"the quick brown fox"

    async def _fake_resolve(source: dict, **_kw) -> None:  # type: ignore[type-arg]
        source["_resolved_byte_length"] = len(raw)
        source["_resolved_md5_hex"] = hashlib.md5(raw).hexdigest()
        source["_resolved_content_type"] = "text/plain"
        source["_resolved_bytes"] = raw

    monkeypatch.setattr(_attachments, "_resolve_gcs_attachment", _fake_resolve)

    files = await _extract(
        [
            {
                "role": "user",
                "parts": [{"fileData": {"fileUri": "gs://b/o.txt"}}],
            }
        ],
        include_raw_content=True,
    )
    assert len(files) == 1
    f = files[0]
    assert f.content_hashes == {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "sha1": hashlib.sha1(raw).hexdigest(),
        "md5": hashlib.md5(raw).hexdigest(),
    }
    assert f.byte_length == len(raw)
    assert f.redacted_content == "the quick brown fox"


async def test_file_data_oversized_range_get_no_hash_only_snippet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in + oversized: no hash (bytes incomplete), redacted_content shows
    head+tail elided by ``truncate_middle``. byte_length comes from HEAD."""
    async def _fake_resolve(source: dict, **_kw) -> None:  # type: ignore[type-arg]
        source["_resolved_byte_length"] = 10_000_000
        source["_resolved_md5_hex"] = "deadbeef"
        source["_resolved_content_type"] = "text/plain"
        source["_resolved_head_bytes"] = b"HEADHEADHEAD"
        source["_resolved_tail_bytes"] = b"TAILTAILTAIL"

    monkeypatch.setattr(_attachments, "_resolve_gcs_attachment", _fake_resolve)

    files = await _extract(
        [
            {
                "role": "user",
                "parts": [{"fileData": {"fileUri": "gs://b/huge.log"}}],
            }
        ],
        include_raw_content=True,
        max_content_size=100,
    )
    assert len(files) == 1
    f = files[0]
    assert f.content_hashes is None  # partial fetch → no hash
    assert f.byte_length == 10_000_000
    assert f.redacted_content is not None
    assert "HEAD" in f.redacted_content
    assert "TAIL" in f.redacted_content
    assert "…" in f.redacted_content


async def test_file_data_dedupes_repeated_uris(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two fileData parts pointing at the same gs:// URI collapse to one entry
    via ``finalize``'s multi-alg dedup on the shared md5."""
    raw = b"same file"

    async def _fake_resolve(source: dict, **_kw) -> None:  # type: ignore[type-arg]
        source["_resolved_byte_length"] = len(raw)
        source["_resolved_md5_hex"] = hashlib.md5(raw).hexdigest()
        source["_resolved_content_type"] = "text/plain"

    monkeypatch.setattr(_attachments, "_resolve_gcs_attachment", _fake_resolve)

    files = await _extract(
        [
            {
                "role": "user",
                "parts": [
                    {"fileData": {"fileUri": "gs://b/x.txt"}},
                    {"fileData": {"fileUri": "gs://b/x.txt"}},
                ],
            }
        ]
    )
    assert len(files) == 1
