"""Bedrock Converse document/image attachment extraction → AIAccessedFile.

Walks the raw MIL record's inputBodyJson for ``{document: {...}}`` and
``{image: {...}}`` blocks in messages, decodes inline base64 bytes, or
resolves ``{s3Location: {uri}}`` sources via HeadObject + optional
GetObject (see ``s3.py::_resolve_s3_attachment``). Returns a list of
``AIAccessedFile`` entries for consumption by the shared finalize step
via ``normalized.accessed_files.extend(...)``.

This is vendor-specific — Converse's document/image block shape is
Bedrock-only. Other vendors' equivalents (Vertex ``inlineData``, OpenAI
Responses ``input_image``, etc.) get their own extractors in their own
forwarder subprojects following the same pattern.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
from typing import Any

from slashid_ai_forwarder_core.content_utils import truncate_middle
from slashid_ai_forwarder_core.events import AIAccessedFile

from .s3 import MAX_PARALLEL_FETCHES, _resolve_s3_attachment

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


async def extract_converse_attachments(
    record: dict[str, Any],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    """Walk Converse messages for document/image blocks → AIAccessedFile[].

    S3-sourced attachments are resolved concurrently up-front (bounded by
    ``MAX_PARALLEL_FETCHES``). Inline base64 attachments are decoded
    synchronously as messages are walked. Only fresh-region messages
    count — attachments in prior turns were reported on earlier events.
    """
    body = (record.get("input") or {}).get("inputBodyJson")
    if not isinstance(body, dict):
        return []
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    if not messages:
        return []

    last_assistant = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1,
    )
    fresh_messages = messages[last_assistant + 1 :]

    # Collect all S3 source blocks from fresh messages so we can resolve
    # them concurrently before building the file list.
    s3_sources: list[dict[str, Any]] = []
    for msg in fresh_messages:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            for key in ("document", "image"):
                item = block.get(key)
                if isinstance(item, dict):
                    src = item.get("source") or {}
                    if "s3Location" in src or "s3Uri" in src:
                        s3_sources.append(src)

    if s3_sources:
        sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

        async def _guarded(src: dict[str, Any]) -> None:
            async with sem:
                await _resolve_s3_attachment(src, max_content_size=max_content_size)

        await asyncio.gather(*(_guarded(src) for src in s3_sources))

    return _build_files_from_messages(
        fresh_messages,
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )


def _mime_from_name(name: str | None) -> str | None:
    if not name:
        return None
    mt, _ = mimetypes.guess_type(name)
    return mt or None


def _decode_b64(val: Any) -> bytes | None:
    if not val:
        return None
    try:
        return base64.b64decode(val)
    except Exception:
        return None


def _build_files_from_messages(
    fresh_messages: list[dict[str, Any]],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    files: list[AIAccessedFile] = []
    seen: set[tuple[str | None, str | None]] = set()

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
        key = (name, content_hashes["sha256"] if content_hashes else None)
        if key in seen:
            return
        seen.add(key)
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

    for msg in fresh_messages:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue

            if "document" in block:
                doc = block["document"] or {}
                fmt = doc.get("format") or None
                source = doc.get("source") or {}
                name = doc.get("name") or None
                if "bytes" in source:
                    raw_bytes = _decode_b64(source["bytes"])
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
                    uri = (source.get("s3Location") or {}).get("uri") or source.get("s3Uri") or None
                    file_name = name or uri
                    mime_hint = _mime_from_name(file_name) or _mime_from_name(uri)
                    if "_resolved_byte_length" not in source:
                        # HEAD failed — emit a stub so callers know the file was referenced.
                        _add(name=file_name, media_type=mime_hint, raw_bytes=None)
                    else:
                        media_type = (
                            _DOC_MIME.get(fmt, f"application/{fmt}")
                            if fmt
                            else source.get("_resolved_content_type") or mime_hint
                        )
                        _add(
                            name=file_name,
                            media_type=media_type,
                            raw_bytes=source.get("_resolved_bytes"),
                            length=source.get("_resolved_byte_length"),
                            partial_head=source.get("_resolved_head_bytes"),
                            partial_tail=source.get("_resolved_tail_bytes"),
                        )

            elif "image" in block:
                img = block["image"] or {}
                fmt = img.get("format") or None
                source = img.get("source") or {}
                if "bytes" in source:
                    raw_bytes = _decode_b64(source["bytes"])
                    _add(
                        name=None,
                        media_type=_DOC_MIME.get(fmt, f"image/{fmt}") if fmt else None,
                        raw_bytes=raw_bytes,
                    )
                else:
                    uri = (source.get("s3Location") or {}).get("uri") or source.get("s3Uri") or None
                    if "_resolved_byte_length" not in source:
                        _add(name=uri, media_type=_mime_from_name(uri), raw_bytes=None)
                    else:
                        media_type = (
                            _DOC_MIME.get(fmt, f"image/{fmt}")
                            if fmt
                            else source.get("_resolved_content_type") or _mime_from_name(uri)
                        )
                        _add(
                            name=uri,
                            media_type=media_type,
                            raw_bytes=source.get("_resolved_bytes"),
                            length=source.get("_resolved_byte_length"),
                            partial_head=source.get("_resolved_head_bytes"),
                            partial_tail=source.get("_resolved_tail_bytes"),
                        )

    return files
