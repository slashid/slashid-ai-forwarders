"""Gemini ``inlineData`` / ``fileData`` extraction → AIAccessedFile[].

Called by ``gemini/normalize.py::to_normalized_invocation`` inside the
canonical conversion. Walks the fresh-turn window (messages AFTER the
last ``role="model"`` message) for ``GeminiInlineDataPart`` and
``GeminiFileDataPart`` variants:

- ``inlineData``: base64-decode synchronously, produce
  ``sha256``/``sha1``/``md5`` hashes, ``byte_length``, and (opt-in)
  ``redacted_content``.
- ``fileData``: ``gs://`` URI → HEAD + optional GET via
  ``gcs._resolve_gcs_attachment`` (bounded by ``MAX_PARALLEL_FETCHES``).
  Default path: ``md5`` (from GCS metadata) + ``byte_length`` +
  ``media_type``. Opt-in via ``include_raw_content=True``: full body
  fetched → all three hashes computed locally, ``redacted_content``
  populated. Oversized fetch: head+tail Range GETs, no hash (partial
  fetch), only elided ``redacted_content``.

Two-pass structure mirrors ``converse/attachments.py``: Pass 1 builds
source dicts (only fileData needs a resolution store), fires resolvers
concurrently; Pass 2 walks the parts a second time and assembles the
``AIAccessedFile`` list from the stashed keys.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes

from ...config_base import BaseConfig
from ...content_utils import truncate_middle
from ...events import AIAccessedFile
from ..turn import after_last_assistant
from .gcs import MAX_PARALLEL_FETCHES, _resolve_gcs_attachment
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

    fileData sources are resolved concurrently up-front (bounded by
    ``MAX_PARALLEL_FETCHES``). Inline base64 attachments are decoded
    synchronously as the second pass walks messages.

    Duplicates in the fresh-turn window (same file referenced twice)
    are NOT deduped here — the shared ``finalize`` pass canonicalizes
    ``normalized.accessed_files`` after every vendor extractor runs.
    """
    fresh_messages = after_last_assistant(request.contents, role_value="model")
    if not fresh_messages:
        return []

    # Pass 1: build one mutable resolution-store dict per fileData part.
    # The resolver stamps ``_resolved_*`` keys on the same dict Pass 2
    # will consume — we key by object identity via the (part, dict) tuple.
    #
    # inlineData parts get no source dict (decoded synchronously in Pass 2).
    file_data_dicts: list[tuple[GeminiFileData, dict]] = []  # type: ignore[type-arg]
    for msg in fresh_messages:
        for part in msg.parts:
            if isinstance(part, GeminiFileDataPart):
                file_data_dicts.append((part.fileData, {"fileUri": part.fileData.fileUri}))

    if file_data_dicts:
        sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

        async def _guarded(src: dict) -> None:  # type: ignore[type-arg]
            async with sem:
                await _resolve_gcs_attachment(
                    src,
                    max_content_size=config.max_content_size,
                    include_raw_content=config.include_raw_content,
                )

        await asyncio.gather(*(_guarded(src) for _fd, src in file_data_dicts))

    # Pass 2: walk parts in order, assembling AIAccessedFile list.
    # For fileData parts, pop the next resolution dict off a queue (order
    # matches Pass 1 since Pass 1 iterated the same fresh_messages).
    resolution_iter = iter(file_data_dicts)
    files: list[AIAccessedFile] = []
    for msg in fresh_messages:
        for part in msg.parts:
            match part:
                case GeminiInlineDataPart():
                    files.append(_from_inline_data(part.inlineData, config))
                case GeminiFileDataPart():
                    _fd, src_dict = next(resolution_iter)
                    files.append(_from_file_data(part.fileData, src_dict, config))
                # Other part types: not attachments.
    return files


def _from_inline_data(blob: GeminiBlob, config: BaseConfig) -> AIAccessedFile:
    raw_bytes = _decode_b64(blob.data)
    if raw_bytes is None:
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
        name=None,
        content_hashes=content_hashes,
        media_type=blob.mimeType or None,
        byte_length=len(raw_bytes),
        redacted_content=redacted,
    )


def _from_file_data(
    file_data: GeminiFileData,
    src_dict: dict,  # type: ignore[type-arg]
    config: BaseConfig,
) -> AIAccessedFile:
    """Build an ``AIAccessedFile`` from a fileData part + its resolution dict.

    Three cases, in priority order:
      1. Full bytes stashed (``_resolved_bytes``) → all three hashes computed
         locally, ``redacted_content`` = full decoded text (opt-in).
      2. Head + tail stashed (``_resolved_head_bytes`` / ``_resolved_tail_bytes``)
         → md5 preserved from GCS metadata (authoritative on the full object
         even under partial fetch); sha256/sha1 uncomputable so omitted.
         ``redacted_content`` = head + "…" + tail middle-elided by
         ``truncate_middle`` (opt-in only, since Pass 1 only fetches ranges
         when include_raw_content=True).
      3. Metadata only (``_resolved_byte_length`` + ``_resolved_md5_hex``) →
         ``content_hashes = {"md5": <hex>}``, no ``redacted_content``.

    Fallback (nothing stashed → HEAD failure): stub matching Phase 3.1 —
    URI as name, media_type from part or filename guess, everything else None.
    """
    uri = file_data.fileUri
    resolved_len = src_dict.get("_resolved_byte_length")
    media_type = (
        file_data.mimeType or src_dict.get("_resolved_content_type") or _mime_from_name(uri)
    )

    if resolved_len is None:
        # HEAD failed — emit a stub so callers see the reference.
        return AIAccessedFile(
            name=uri,
            content_hashes=None,
            media_type=media_type,
            byte_length=None,
            redacted_content=None,
        )

    raw_bytes = src_dict.get("_resolved_bytes")
    head_bytes = src_dict.get("_resolved_head_bytes")
    tail_bytes = src_dict.get("_resolved_tail_bytes")

    content_hashes: dict[str, str] | None
    redacted: str | None = None

    if isinstance(raw_bytes, (bytes, bytearray)):
        # Full body fetched: compute all three hashes locally + populate redacted_content.
        content_hashes = {
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "sha1": hashlib.sha1(raw_bytes).hexdigest(),
            "md5": hashlib.md5(raw_bytes).hexdigest(),
        }
        if config.include_raw_content:
            redacted = truncate_middle(
                bytes(raw_bytes).decode(errors="replace"), config.max_content_size
            )
    elif isinstance(head_bytes, (bytes, bytearray)) and isinstance(tail_bytes, (bytes, bytearray)):
        # Partial fetch (oversized): sha256/sha1 uncomputable (bytes incomplete),
        # but md5 from GCS metadata is authoritative on the full object — keep it.
        md5_hex = src_dict.get("_resolved_md5_hex")
        content_hashes = {"md5": md5_hex} if isinstance(md5_hex, str) else None
        if config.include_raw_content:
            head_str = bytes(head_bytes).decode(errors="replace")
            tail_str = bytes(tail_bytes).decode(errors="replace")
            redacted = truncate_middle(head_str + "…" + tail_str, config.max_content_size)
    else:
        # Metadata only: md5 from GCS (converted from base64 to hex during resolve).
        md5_hex = src_dict.get("_resolved_md5_hex")
        content_hashes = {"md5": md5_hex} if isinstance(md5_hex, str) else None

    return AIAccessedFile(
        name=uri,
        content_hashes=content_hashes,
        media_type=media_type,
        byte_length=resolved_len,
        redacted_content=redacted,
    )


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
