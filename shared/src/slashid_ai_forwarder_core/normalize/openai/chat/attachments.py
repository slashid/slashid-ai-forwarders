"""Chat Completions ``image_url`` / ``file`` parts → AIAccessedFile[].

Inline ``data:`` URLs are decoded and hashed; ``s3://`` references (the only
persisted source the endpoint accepts) go through the Converse S3 resolver.
Only the fresh turn contributes, as in Converse. The ``aioboto3`` behind the
resolver is imported on first use.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from dataclasses import dataclass, field
from typing import Any

from ....config_base import BaseConfig
from ....content_utils import truncate_middle
from ....events import AIAccessedFile
from ..._fetch_semaphore import get_fetch_semaphore
from ...turn import after_last_assistant
from ..data_url import parse_data_url
from .schema import ChatFilePart, ChatImagePart, ChatRequest

_S3 = "s3://"


@dataclass
class _Ref:
    name: str | None
    media_type: str | None
    data: bytes | None = None
    s3: dict[str, Any] | None = field(default=None)


async def extract_attachments(request: ChatRequest, *, config: BaseConfig) -> list[AIAccessedFile]:
    refs = [
        ref
        for message in after_last_assistant(request.messages)
        if isinstance(message.content, list)
        for part in message.content
        if (ref := _ref(part)) is not None
    ]
    pending = [r.s3 for r in refs if r.s3 is not None]
    if pending:
        from ...converse import s3

        sem = get_fetch_semaphore()

        async def resolve(source: dict[str, Any]) -> None:
            async with sem:
                await s3._resolve_s3_attachment(source, max_content_size=config.max_content_size)

        await asyncio.gather(*(resolve(source) for source in pending))
    return [_accessed_file(ref, config) for ref in refs]


def _ref(part: object) -> _Ref | None:
    match part:
        case ChatImagePart():
            return _from_url(part.image_url.url, name=None, stem="image")
        case ChatFilePart():
            source = part.file.file_data or part.file.file_id
            return _from_url(source, name=part.file.filename, stem="file")
    return None


def _from_url(url: str | None, *, name: str | None, stem: str) -> _Ref | None:
    if url is not None and url.startswith(_S3):
        return _Ref(name=name or url, media_type=None, s3={"s3Uri": url})
    if (parsed := parse_data_url(url)) is None:
        return None
    media_type, data = parsed
    ext = mimetypes.guess_extension(media_type) if media_type else None
    return _Ref(name=name or f"{stem}{ext or ''}", media_type=media_type, data=data)


def _accessed_file(ref: _Ref, config: BaseConfig) -> AIAccessedFile:
    data, length, media_type = ref.data, None, ref.media_type
    if ref.s3 is not None:
        data = ref.s3.get("_resolved_bytes")
        length = ref.s3.get("_resolved_byte_length")
        media_type = ref.s3.get("_resolved_content_type") or _guess_type(ref.name)
    return AIAccessedFile(
        name=ref.name,
        media_type=media_type,
        byte_length=len(data) if data is not None else length,
        content_hashes=_hashes(data),
        redacted_content=_redacted(data, media_type, config),
        provenance="attachment",
    )


def _guess_type(name: str | None) -> str | None:
    return mimetypes.guess_type(name)[0] if name else None


def _hashes(data: bytes | None) -> dict[str, str] | None:
    if data is None:
        return None
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "md5": hashlib.md5(data).hexdigest(),
    }


def _redacted(data: bytes | None, media_type: str | None, config: BaseConfig) -> str | None:
    if data is None or not config.include_raw_content or not (media_type or "").startswith("text/"):
        return None
    return truncate_middle(data.decode(errors="replace"), config.max_content_size)
