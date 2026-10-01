"""Per-session log and cursors, their lock, and eviction."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slashid_ai_forwarder_core.platform.checkpoint import Checkpoint

from .cursor import RolloutCursor
from .log import LogReset, SessionLog

HOOK_IDLE = timedelta(minutes=10)
_ROOTS = ("sessions", "archived_sessions")


def locate(session_id: str, transcript_path: Path | None, codex_home: Path) -> Path | None:
    """The hook's ``transcript_path``, else ``*-<id>.jsonl`` or ``*-<id>_*.jsonl``
    under ``sessions/`` and ``archived_sessions/``, the most recently modified
    if several (measured: a session can leave an abandoned earlier rollout
    under the exact name)."""
    if transcript_path is not None and transcript_path.is_file():
        return transcript_path
    found = [
        path
        for root in _ROOTS
        for pattern in (f"*-{session_id}.jsonl", f"*-{session_id}_*.jsonl")
        for path in (codex_home / root).rglob(pattern)
        if path.is_file()
    ]
    return max(found, key=lambda path: path.stat().st_mtime, default=None)


@dataclass(eq=False)
class Session:
    session_id: str
    log: SessionLog
    # Preflight; always at the end of the log.
    head: RolloutCursor
    # Collection; past the watermark.
    send: RolloutCursor
    _open: Callable[[Path], tuple[SessionLog, RolloutCursor, RolloutCursor]]
    lock: threading.Lock = field(default_factory=threading.Lock)
    # True from any hook until ``SessionEnd``; false for sweep-loaded sessions.
    session_started: bool = False
    last_hook_at: datetime | None = None
    batch_in_flight: bool = False

    def refresh(self) -> int:
        """Read appended lines and move the head to the end. A shrunk or
        replaced file is read again from byte 0 with fresh cursors."""
        try:
            added = self.log.refresh()
        except LogReset:
            self.log, self.head, self.send = self._open(self.log.path)
            return len(self.log.lines)
        self.head.advance_to_end()
        return added


class SessionCache:
    def __init__(
        self,
        *,
        codex_home: Path,
        load_watermark: Callable[[str], Checkpoint],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._codex_home = codex_home
        self._load_watermark = load_watermark
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def locate_parent(self, thread_id: str) -> Path | None:
        return locate(thread_id, None, self._codex_home)

    def get(self, session_id: str, path: Path) -> Session:
        """The cached session, else one read from byte 0 with its send cursor
        past the watermark. Raises ``OSError`` if the rollout is unreadable."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                # Archiving moves the file; its identity is unchanged.
                session.log.path = path
                return session

        def open_log(path: Path) -> tuple[SessionLog, RolloutCursor, RolloutCursor]:
            log = SessionLog.open(path, self.locate_parent)
            log.refresh()
            head = RolloutCursor(log)
            head.advance_to_end()
            send = RolloutCursor(log)
            send.skip_to(self._load_watermark(session_id))
            return log, head, send

        log, head, send = open_log(path)
        built = Session(session_id, log, head, send, open_log)
        with self._lock:
            return self._sessions.setdefault(session_id, built)

    def touch_hook(self, session_id: str) -> None:
        with self._lock:
            if (session := self._sessions.get(session_id)) is not None:
                session.session_started = True
                session.last_hook_at = self._clock()

    def end(self, session_id: str) -> None:
        with self._lock:
            if (session := self._sessions.get(session_id)) is not None:
                session.session_started = False

    def evictable(self, session: Session) -> bool:
        idle = (
            not session.session_started
            or session.last_hook_at is None
            or self._clock() > session.last_hook_at + HOOK_IDLE
        )
        return session.send.at_end and not session.batch_in_flight and idle

    def evict(self) -> list[str]:
        """Drop evictable sessions not in use; a dropped one is rebuilt from
        byte 0 on its next ``get``."""
        evicted: list[str] = []
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if not session.lock.acquire(blocking=False):
                    continue
                try:
                    if self.evictable(session):
                        del self._sessions[session_id]
                        evicted.append(session_id)
                finally:
                    session.lock.release()
        return evicted
