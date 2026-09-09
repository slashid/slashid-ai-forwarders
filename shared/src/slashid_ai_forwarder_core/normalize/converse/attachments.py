"""Bedrock Converse document/image attachment extraction → AIAccessedFile[].

Called by ``converse/normalize.py::to_normalized_invocation`` inside the
canonical conversion. Walks ``ConverseRequestBody.messages`` for
``ConverseDocumentBlock`` / ``ConverseImageBlock`` variants, decodes
inline base64 sources synchronously, and resolves ``{s3Location}`` /
``{s3Uri}`` sources concurrently via ``s3._resolve_s3_attachment``
(bounded by ``MAX_PARALLEL_FETCHES``).

Only fresh-turn windows contribute — messages AFTER the last assistant
message — matching the pre-Phase-2.2 behaviour of the retired
``bedrock/converse_attachments.py``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes

from ...config_base import BaseConfig
from ...content_utils import truncate_middle
from ...events import AIAccessedFile
from .._fetch_semaphore import get_fetch_semaphore
from ..turn import after_last_assistant
from .s3 import _resolve_s3_attachment
from .schema import (
    ConverseDocumentBlock,
    ConverseDocumentSource,
    ConverseImageBlock,
    ConverseImageSource,
    ConverseRequestBody,
)

# Bedrock Converse format enum → IANA media types.
# Document canonical list:
# https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_DocumentBlock.html
# Image canonical list:
# https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ImageBlock.html
_DOC_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


async def extract_attachments(
    request: ConverseRequestBody,
    *,
    config: BaseConfig,
) -> list[AIAccessedFile]:
    """Walk Converse request messages for document/image blocks → AIAccessedFile[].

    Fresh-turn semantics: only messages AFTER the last assistant message
    contribute — earlier attachments were reported on prior events.

    S3-sourced attachments are resolved concurrently up-front (bounded by
    the shared ``get_fetch_semaphore``). Inline base64 attachments are
    decoded synchronously as the second pass walks messages.

    Duplicates in the fresh-turn window (same file referenced twice) are
    NOT deduped here — the shared ``finalize`` pass canonicalizes
    ``normalized.accessed_files`` after every vendor extractor runs.
    """
    fresh_messages = after_last_assistant(request.messages)
    if not fresh_messages:
        return []

    # Pass 1: build one mutable "resolution-store" dict per document/image
    # block. The resolver stamps ``_resolved_*`` keys on the same dict Pass 2
    # will consume — we key by object identity via the (block, dict) tuple
    # binding.
    #
    # Only blocks with S3 sources get resolved; inline-bytes blocks get an
    # empty dict (no S3 fetch needed, decoded synchronously in Pass 2).
    block_dicts: list[tuple[object, dict]] = []  # type: ignore[type-arg]
    s3_source_dicts: list[dict] = []  # type: ignore[type-arg]
    for msg in fresh_messages:
        for block in msg.content:
            match block:
                case ConverseDocumentBlock():
                    src_dict = _resolution_dict(block.document.source)
                    block_dicts.append((block, src_dict))
                    if _has_s3_source(block.document.source):
                        s3_source_dicts.append(src_dict)
                case ConverseImageBlock():
                    src_dict = _resolution_dict(block.image.source)
                    block_dicts.append((block, src_dict))
                    if _has_s3_source(block.image.source):
                        s3_source_dicts.append(src_dict)

    if s3_source_dicts:
        sem = get_fetch_semaphore()

        async def _guarded(src: dict) -> None:  # type: ignore[type-arg]
            async with sem:
                await _resolve_s3_attachment(src, max_content_size=config.max_content_size)

        await asyncio.gather(*(_guarded(src) for src in s3_source_dicts))

    # Pass 2: build the file list, re-using the same source dicts Pass 1
    # stamped resolutions onto.
    return _build_files_from_block_dicts(
        block_dicts,
        include_raw_content=config.include_raw_content,
        max_content_size=config.max_content_size,
    )


def _resolution_dict(source: ConverseDocumentSource | ConverseImageSource) -> dict:  # type: ignore[type-arg]
    """Build the mutable dict passed into ``_resolve_s3_attachment``.

    The resolver only reads ``s3Location.uri`` / ``s3Uri`` from the dict; we
    seed those and let the resolver stamp ``_resolved_*`` keys on top —
    Pass 2 reads back from the same dict via the tuple binding.
    """
    d: dict = {}  # type: ignore[type-arg]
    if source.s3Location is not None:
        d["s3Location"] = {"uri": source.s3Location.uri}
    if source.s3Uri is not None:
        d["s3Uri"] = source.s3Uri
    return d


def _has_s3_source(source: ConverseDocumentSource | ConverseImageSource) -> bool:
    return source.s3Location is not None or source.s3Uri is not None


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


def _build_files_from_block_dicts(
    block_dicts: list[tuple[object, dict]],  # type: ignore[type-arg]
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    files: list[AIAccessedFile] = []

    def _add(
        name: str | None,
        media_type: str | None,
        raw_bytes: bytes | None,
        length: int | None = None,
        partial_head: bytes | None = None,
        partial_tail: bytes | None = None,
    ) -> None:
        # Full bytes → stable hashes. Partial fetch → no hashes (bytes incomplete).
        content_hashes: dict[str, str] | None
        if raw_bytes is not None:
            content_hashes = {
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "sha1": hashlib.sha1(raw_bytes).hexdigest(),
                "md5": hashlib.md5(raw_bytes).hexdigest(),
            }
        else:
            content_hashes = None
        redacted: str | None = None
        if include_raw_content:
            if raw_bytes is not None:
                redacted = truncate_middle(raw_bytes.decode(errors="replace"), max_content_size)
            elif partial_head is not None and partial_tail is not None:
                head_str = partial_head.decode(errors="replace")
                tail_str = partial_tail.decode(errors="replace")
                combined = head_str + "…" + tail_str
                redacted = truncate_middle(combined, max_content_size)
        files.append(
            AIAccessedFile(
                name=name,
                content_hashes=content_hashes,
                media_type=media_type,
                byte_length=len(raw_bytes) if raw_bytes is not None else length,
                redacted_content=redacted,
            )
        )

    for block, src_dict in block_dicts:
        match block:
            case ConverseDocumentBlock():
                doc = block.document
                fmt = doc.format
                name = doc.name
                if doc.source.bytes is not None:
                    raw_bytes = _decode_b64(doc.source.bytes)
                    _add(
                        name=name,
                        media_type=(
                            _DOC_MIME.get(fmt, f"application/{fmt}")
                            if fmt
                            else _mime_from_name(name)
                        ),
                        raw_bytes=raw_bytes,
                    )
                else:
                    uri = (
                        (doc.source.s3Location.uri if doc.source.s3Location else None)
                        or doc.source.s3Uri
                        or None
                    )
                    file_name = name or uri
                    mime_hint = _mime_from_name(file_name) or _mime_from_name(uri)
                    resolved_len = src_dict.get("_resolved_byte_length")
                    if resolved_len is None:
                        # HEAD failed — emit a stub so callers know the file was referenced.
                        _add(name=file_name, media_type=mime_hint, raw_bytes=None)
                    else:
                        media_type = (
                            _DOC_MIME.get(fmt, f"application/{fmt}")
                            if fmt
                            else src_dict.get("_resolved_content_type") or mime_hint
                        )
                        _add(
                            name=file_name,
                            media_type=media_type,
                            raw_bytes=src_dict.get("_resolved_bytes"),
                            length=resolved_len,
                            partial_head=src_dict.get("_resolved_head_bytes"),
                            partial_tail=src_dict.get("_resolved_tail_bytes"),
                        )

            case ConverseImageBlock():
                img = block.image
                fmt = img.format
                if img.source.bytes is not None:
                    raw_bytes = _decode_b64(img.source.bytes)
                    _add(
                        name=None,
                        media_type=_DOC_MIME.get(fmt, f"image/{fmt}") if fmt else None,
                        raw_bytes=raw_bytes,
                    )
                else:
                    uri = (
                        (img.source.s3Location.uri if img.source.s3Location else None)
                        or img.source.s3Uri
                        or None
                    )
                    resolved_len = src_dict.get("_resolved_byte_length")
                    if resolved_len is None:
                        _add(name=uri, media_type=_mime_from_name(uri), raw_bytes=None)
                    else:
                        media_type = (
                            _DOC_MIME.get(fmt, f"image/{fmt}")
                            if fmt
                            else src_dict.get("_resolved_content_type") or _mime_from_name(uri)
                        )
                        _add(
                            name=uri,
                            media_type=media_type,
                            raw_bytes=src_dict.get("_resolved_bytes"),
                            length=resolved_len,
                            partial_head=src_dict.get("_resolved_head_bytes"),
                            partial_tail=src_dict.get("_resolved_tail_bytes"),
                        )

    return files
