"""``SessionLog``: a rollout's parsed lines, read incrementally."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .rollout import HistoryBase, RolloutLine, RolloutLineError, SessionMeta, parse_line

log = logging.getLogger(__name__)

LocateParent = Callable[[str], Path | None]

# Forks of forks; a cycle stops here.
_MAX_FORK_DEPTH = 16


class LogReset(Exception):
    """The file shrank below the offset or was replaced: read it again from byte 0."""


class LogLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    line: RolloutLine
    # From a fork's parent rollout.
    inherited: bool
    # Byte offset after the line in its own file.
    offset_after: int


class SessionLog:
    """Lines read so far; ``refresh`` only appends. No handle stays open
    between refreshes (on Windows it would block archiving the file)."""

    def __init__(
        self,
        path: Path,
        locate_parent: LocateParent,
        *,
        limit: int | None = None,
        depth: int = 0,
    ) -> None:
        self.path = path
        self.lines: list[LogLine] = []
        self.offset = 0
        self.history_truncated = False
        # Unparseable lines, for ``daemon.log``.
        self.skipped = 0
        self._locate_parent = locate_parent
        self._limit = limit
        self._depth = depth
        self._partial = b""
        self._identity: tuple[int, int] | None = None
        self._started = False

    @classmethod
    def open(cls, path: Path, locate_parent: LocateParent) -> SessionLog:
        return cls(path, locate_parent)

    def refresh(self) -> int:
        """Read what was appended; returns how many lines were added."""
        with self.path.open("rb") as f:
            stat = os.fstat(f.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if self._identity is None:
                self._identity = identity
            elif identity != self._identity or stat.st_size < self.offset:
                raise LogReset(str(self.path))
            f.seek(self.offset)
            data = f.read() if self._limit is None else f.read(max(0, self._limit - self.offset))
        start = self.offset - len(self._partial)
        self.offset += len(data)
        *complete, self._partial = (self._partial + data).split(b"\n")
        added = 0
        for raw in complete:
            start += len(raw) + 1
            if raw.strip():
                added += self._append(raw, start)
        return added

    def _append(self, raw: bytes, offset_after: int) -> int:
        try:
            line = parse_line(raw)
        except RolloutLineError as exc:
            self.skipped += 1
            log.warning("skipped rollout line in %s at byte %d: %s", self.path, offset_after, exc)
            return 0
        if line is None:
            return 0
        added = 0
        if not self._started:
            self._started = True
            if isinstance(line.payload, SessionMeta) and line.payload.history_base is not None:
                added = self._load_base(line.payload.history_base)
        self.lines.append(LogLine(line=line, inherited=False, offset_after=offset_after))
        return added + 1

    def _load_base(self, base: HistoryBase) -> int:
        """A fork's history is its parent's rollout up to ``end_byte_offset``."""
        path = self._locate_parent(base.thread_id) if self._depth < _MAX_FORK_DEPTH else None
        if path is None:
            self.history_truncated = True
            return 0
        parent = SessionLog(
            path, self._locate_parent, limit=base.end_byte_offset, depth=self._depth + 1
        )
        try:
            parent.refresh()
        except OSError as exc:
            log.warning("fork parent %s unreadable: %s", path, exc)
            self.history_truncated = True
            return 0
        self.history_truncated = parent.history_truncated
        self.skipped += parent.skipped
        self.lines.extend(
            LogLine(line=line.line, inherited=True, offset_after=line.offset_after)
            for line in parent.lines
        )
        return len(parent.lines)
