"""Gemini ``inlineData`` / ``fileData`` extraction → AIAccessedFile[].

Called by ``gemini/normalize.py::to_normalized_invocation`` inside the
canonical conversion. Walks the fresh-turn window (messages AFTER the
last ``role="model"`` message) for ``GeminiInlineDataPart`` and
``GeminiFileDataPart`` variants:

- ``inlineData``: base64-decode synchronously, produce
  ``sha256``/``sha1``/``md5`` hashes, ``byte_length``, and (opt-in)
  ``redacted_content``.
- ``fileData``: ``gs://`` URI → stub-only in Phase 3.1. No hash, no
  byte_length; name = URI, media_type = ``mimeType``. Full GCS fetch
  lands in a follow-up phase under a ``[gcs]`` extras group.

Signed ``async`` for uniformity with ``converse/attachments.py`` —
Phase 3.1 does no I/O, but the GCS follow-up will fetch under a
concurrency bound the same way Converse fetches S3.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes

from ...config_base import BaseConfig
from ...content_utils import truncate_middle
from ...events import AIAccessedFile
from ..turn import after_last_assistant
from .schema import (
    GeminiBlob,
    GeminiFileData,
    GeminiFileDataPart,
    GeminiInlineDataPart,
    GeminiRequestBody,
)


async def extract_attachments(
    request: GeminiRequestBody,
    *,
    config: BaseConfig,
) -> list[AIAccessedFile]:
    """Walk Gemini request contents for inlineData/fileData parts → AIAccessedFile[].

    Fresh-turn semantics: only messages AFTER the last ``role="model"``
    message contribute — earlier attachments were reported on prior
    events. Uses ``after_last_assistant(..., role_value="model")`` per
    Gemini's role naming.
    """
    fresh_messages = after_last_assistant(request.contents, role_value="model")
    if not fresh_messages:
        return []

    files: list[AIAccessedFile] = []
    seen: set[tuple[str | None, str | None]] = set()
    for msg in fresh_messages:
        for part in msg.parts:
            match part:
                case GeminiInlineDataPart():
                    entry = _from_inline_data(part.inlineData, config)
                    _dedupe_append(files, seen, entry)
                case GeminiFileDataPart():
                    entry = _from_file_data(part.fileData)
                    _dedupe_append(files, seen, entry)
                # Other part types: not attachments.
    return files


def _from_inline_data(blob: GeminiBlob, config: BaseConfig) -> AIAccessedFile:
    raw_bytes = _decode_b64(blob.data)
    if raw_bytes is None:
        # Corrupt base64 — emit a stub so callers see the reference.
        return AIAccessedFile(
            name=None,
            content_hashes=None,
            media_type=blob.mimeType or None,
            byte_length=None,
            redacted_content=None,
        )
    content_hashes = {
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "sha1": hashlib.sha1(raw_bytes).hexdigest(),
        "md5": hashlib.md5(raw_bytes).hexdigest(),
    }
    redacted: str | None = None
    if config.include_raw_content:
        redacted = truncate_middle(raw_bytes.decode(errors="replace"), config.max_content_size)
    return AIAccessedFile(
        name=None,  # inline blobs have no name (matches Converse image behaviour)
        content_hashes=content_hashes,
        media_type=blob.mimeType or None,
        byte_length=len(raw_bytes),
        redacted_content=redacted,
    )


def _from_file_data(file_data: GeminiFileData) -> AIAccessedFile:
    """Stub for a ``gs://`` reference. Full fetch lives in a later phase."""
    return AIAccessedFile(
        name=file_data.fileUri,
        content_hashes=None,
        media_type=file_data.mimeType or _mime_from_name(file_data.fileUri),
        byte_length=None,
        redacted_content=None,
    )


def _dedupe_append(
    files: list[AIAccessedFile],
    seen: set[tuple[str | None, str | None]],
    entry: AIAccessedFile,
) -> None:
    """Dedup key = (name, sha256). Matches Converse behaviour."""
    key = (entry.name, entry.content_hashes["sha256"] if entry.content_hashes else None)
    if key in seen:
        return
    seen.add(key)
    files.append(entry)


def _mime_from_name(name: str | None) -> str | None:
    if not name:
        return None
    mt, _ = mimetypes.guess_type(name)
    return mt or None


def _decode_b64(val: str | None) -> bytes | None:
    if not val:
        return None
    try:
        return base64.b64decode(val)
    except Exception:
        return None
