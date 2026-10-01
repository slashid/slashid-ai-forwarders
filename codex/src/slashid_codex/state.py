"""File records: what the preflight hooks hashed, kept until collection
reports the round that consumed them."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol

from slashid_ai_forwarder_core.events import AIAccessedFile

KeyKind = Literal["turn", "call"]


class FileRecordStore(Protocol):
    def put_turn(self, session_id: str, turn_id: str, entries: Sequence[AIAccessedFile]) -> None:
        """A prompt's attachments, keyed by its ``turn_id``."""
        ...

    def put_call(
        self, session_id: str, turn_id: str, tool_use_id: str, entry: AIAccessedFile
    ) -> None:
        """The file a tool call reads, keyed by its ``tool_use_id``."""
        ...

    def for_round(
        self, session_id: str, turn_ids: Sequence[str], tool_ids: Sequence[str]
    ) -> list[AIAccessedFile]:
        """Attachment entries first, then reads, each in write order."""
        ...

    def delete_keys(
        self, session_id: str, turn_ids: Sequence[str], tool_ids: Sequence[str]
    ) -> None: ...

    def delete_turn(self, session_id: str, turn_id: str) -> None:
        """Every record written under ``turn_id``, by either hook."""
        ...

    def delete_older_than(self, cutoff: datetime) -> None: ...


def _placeholders(values: Sequence[str]) -> str:
    return ", ".join("?" * len(values))


class SqliteFileRecordStore:
    """On any connection; ``LocalPlatform.connect()`` in production."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._lock = threading.Lock()
        with self._lock, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS codex_file_records ("
                " session_id TEXT NOT NULL, turn_id TEXT NOT NULL, key_kind TEXT NOT NULL,"
                " key TEXT NOT NULL, entry_json TEXT NOT NULL, written_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS codex_file_records_key"
                " ON codex_file_records (session_id, key_kind, key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS codex_file_records_written_at"
                " ON codex_file_records (written_at)"
            )

    def _put(
        self,
        session_id: str,
        turn_id: str,
        kind: KeyKind,
        key: str,
        entries: Sequence[AIAccessedFile],
    ) -> None:
        written_at = self._clock().timestamp()
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM codex_file_records WHERE session_id = ? AND key_kind = ? AND key = ?",
                (session_id, kind, key),
            )
            self._conn.executemany(
                "INSERT INTO codex_file_records"
                " (session_id, turn_id, key_kind, key, entry_json, written_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (session_id, turn_id, kind, key, entry.model_dump_json(), written_at)
                    for entry in entries
                ],
            )

    def put_turn(self, session_id: str, turn_id: str, entries: Sequence[AIAccessedFile]) -> None:
        self._put(session_id, turn_id, "turn", turn_id, entries)

    def put_call(
        self, session_id: str, turn_id: str, tool_use_id: str, entry: AIAccessedFile
    ) -> None:
        self._put(session_id, turn_id, "call", tool_use_id, [entry])

    def _select(self, session_id: str, kind: KeyKind, keys: Sequence[str]) -> list[AIAccessedFile]:
        if not keys:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_json FROM codex_file_records"
                f" WHERE session_id = ? AND key_kind = ? AND key IN ({_placeholders(keys)})"
                " ORDER BY rowid",
                (session_id, kind, *keys),
            ).fetchall()
        return [AIAccessedFile.model_validate_json(entry_json) for (entry_json,) in rows]

    def for_round(
        self, session_id: str, turn_ids: Sequence[str], tool_ids: Sequence[str]
    ) -> list[AIAccessedFile]:
        return self._select(session_id, "turn", turn_ids) + self._select(
            session_id, "call", tool_ids
        )

    def delete_keys(
        self, session_id: str, turn_ids: Sequence[str], tool_ids: Sequence[str]
    ) -> None:
        with self._lock, self._conn:
            for kind, keys in (("turn", turn_ids), ("call", tool_ids)):
                if keys:
                    self._conn.execute(
                        "DELETE FROM codex_file_records WHERE session_id = ? AND key_kind = ?"
                        f" AND key IN ({_placeholders(keys)})",
                        (session_id, kind, *keys),
                    )

    def delete_turn(self, session_id: str, turn_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM codex_file_records WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            )

    def delete_older_than(self, cutoff: datetime) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM codex_file_records WHERE written_at < ?", (cutoff.timestamp(),)
            )
