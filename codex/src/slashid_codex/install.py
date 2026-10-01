"""When this user's state was first created: the startup sweep's lower bound,
so a fresh install does not backfill."""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .discovery import ensure_state_dir

CREATED_AT = "created_at"


def _read(path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def created_at(state_dir: Path, *, now: datetime | None = None) -> datetime:
    """The first call's time, persisted in ``state_dir/created_at``; an empty
    or garbled file counts as absent. Written aside and linked into place, so
    racing first calls agree."""
    ensure_state_dir(state_dir)
    path = state_dir / CREATED_AT
    if (existing := _read(path)) is not None:
        return existing
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=f".{CREATED_AT}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write((now or datetime.now(UTC)).isoformat())
        try:
            os.link(tmp, path)
        except FileExistsError:
            if _read(path) is None:
                os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    created = _read(path)
    if created is None:
        raise RuntimeError(f"cannot read {path}")
    return created
