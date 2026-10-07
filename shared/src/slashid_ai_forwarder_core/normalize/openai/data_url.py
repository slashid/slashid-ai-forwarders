"""``data:`` URL decoding shared by the OpenAI normalizers."""

from __future__ import annotations

import base64
import binascii

from pydantic_extra_types.mime_types import MimeType

from ..normalized.media_types import parse_media_type


def parse_data_url(url: str | None) -> tuple[MimeType | None, bytes] | None:
    """``(media_type, bytes)`` of a base64 ``data:`` URL; ``None`` when it isn't one."""
    header, sep, payload = (url or "").partition(",")
    if not sep or not header.startswith("data:") or not header.endswith(";base64"):
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None
    return parse_media_type(header.removeprefix("data:").removesuffix(";base64")), data


def decode_data_url(url: str | None) -> tuple[MimeType | None, int | None]:
    """``(media_type, byte_length)`` of a base64 ``data:`` URL; both ``None`` otherwise."""
    parsed = parse_data_url(url)
    return (parsed[0], len(parsed[1])) if parsed else (None, None)
