"""``data:`` URL decoding shared by the OpenAI normalizers."""

from __future__ import annotations

import base64
import binascii

from pydantic_extra_types.mime_types import MimeType

from ..normalized.media_types import parse_media_type


def decode_data_url(url: str | None) -> tuple[MimeType | None, int | None]:
    """``(media_type, byte_length)`` of a base64 ``data:`` URL; both ``None`` otherwise."""
    header, sep, payload = (url or "").partition(",")
    if not sep or not header.startswith("data:") or not header.endswith(";base64"):
        return None, None
    try:
        data = base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None, None
    media_type = parse_media_type(header.removeprefix("data:").removesuffix(";base64"))
    return media_type, len(data)
