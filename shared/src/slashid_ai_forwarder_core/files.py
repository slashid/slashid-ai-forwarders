"""Hashing a local file into an ``AIAccessedFile``, for hooks that check a
file before the model sees it."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Literal

from .events import AIAccessedFile

_CHUNK = 1 << 20


def hash_local_file(
    path: Path, *, max_bytes: int, provenance: Literal["tool_result", "attachment"]
) -> AIAccessedFile:
    """``name`` is the path as given (callers pass it resolved, as tool-result
    entries name files by their path argument). A file over ``max_bytes``,
    missing or unreadable carries no ``content_hashes``: the server counts it
    unchecked instead of failing the request."""
    size, hashes = _size_and_hashes(path, max_bytes)
    return AIAccessedFile(
        name=str(path),
        media_type=mimetypes.guess_type(path.name)[0],
        provenance=provenance,
        byte_length=size,
        content_hashes=hashes,
    )


def _size_and_hashes(path: Path, max_bytes: int) -> tuple[int | None, dict[str, str] | None]:
    try:
        if not path.is_file():
            return None, None
        size = path.stat().st_size
        if size > max_bytes:
            return size, None
        digests = (hashlib.sha256(), hashlib.sha1(), hashlib.md5())
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                for digest in digests:
                    digest.update(chunk)
    except OSError:
        return None, None
    sha256, sha1, md5 = (d.hexdigest() for d in digests)
    return size, {"sha256": sha256, "sha1": sha1, "md5": md5}
