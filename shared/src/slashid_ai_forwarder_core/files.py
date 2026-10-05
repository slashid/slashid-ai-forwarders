"""Hashing a local file into an ``AIAccessedFile``, for hooks that check a
file before the model sees it."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from pathlib import Path
from typing import Literal

from .events import AIAccessedFile

_CHUNK = 1 << 20


def hash_local_file(
    path: Path, *, max_bytes: int, provenance: Literal["tool_result", "attachment"]
) -> AIAccessedFile:
    """Reads ``path`` as given; ``name`` is only its file name, never the
    directory. A file over ``max_bytes``, missing or unreadable carries no
    ``content_hashes``: the server counts it unchecked instead of failing the
    request."""
    size, hashes = _size_and_hashes(path, max_bytes)
    return AIAccessedFile(
        name=path.name,
        media_type=mimetypes.guess_type(path.name)[0],
        provenance=provenance,
        byte_length=size,
        content_hashes=hashes,
    )


def _size_and_hashes(path: Path, max_bytes: int) -> tuple[int | None, dict[str, str] | None]:
    # Nonblocking, so a FIFO swapped in for the file can't hang the open.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None, None
            if info.st_size > max_bytes:
                return info.st_size, None
            digests = (hashlib.sha256(), hashlib.sha1(), hashlib.md5())
            size = 0
            # The file may grow after fstat: cap the bytes actually read.
            while chunk := handle.read(min(_CHUNK, max_bytes + 1 - size)):
                size += len(chunk)
                if size > max_bytes:
                    return max(size, os.fstat(handle.fileno()).st_size), None
                for digest in digests:
                    digest.update(chunk)
    except OSError:
        return None, None
    sha256, sha1, md5 = (d.hexdigest() for d in digests)
    return size, {"sha256": sha256, "sha1": sha1, "md5": md5}
