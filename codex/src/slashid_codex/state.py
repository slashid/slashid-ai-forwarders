"""File records: what the preflight hooks hashed, kept until collection
reports the round that consumed them."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.platform.local.database import DATABASE

from .discovery import ensure_state_dir

KeyKind = Literal["turn", "call"]

# Below the preflight verdict budget.
BUSY_TIMEOUT_MS = 1_000


class RecordStoreBusy(Exception):
    """A write gave up on a locked database. Preflight logs it and still
    returns the verdict; collection retries the batch."""


class FileRecordStore(Protocol):
    def put_turn(self, session_id: str, turn_id: str, entries: Sequence[AIAccessedFile]) -> None:
        """A prompt's attachments, keyed by its ``turn_id``. Raises
        ``RecordStoreBusy``."""
        ...

    def put_call(
        self, session_id: str, turn_id: str, tool_use_id: str, entry: AIAccessedFile
    ) -> None:
        """The file a tool call reads, keyed by its ``tool_use_id``. Raises
        ``RecordStoreBusy``."""
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


def connect(state_dir: Path) -> sqlite3.Connection:
    """``LocalPlatform``'s database file, from a synchronous connection; WAL
    keeps it safe beside the platform's own."""
    ensure_state_dir(state_dir)
    path = state_dir / DATABASE
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _placeholders(values: Sequence[str]) -> str:
    return ", ".join("?" * len(values))


class SqliteFileRecordStore:
    """One connection per thread from ``connect``, each with a
    ``BUSY_TIMEOUT_MS`` busy wait."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._connect = connect
        self._clock = clock
        self._local = threading.local()
        with self._write() as conn:
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

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            # Transactions are explicit in ``_write``; commits any open one.
            conn.autocommit = True
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Raises ``RecordStoreBusy`` past the busy wait."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY:
                raise RecordStoreBusy(str(exc)) from exc
            raise

    def _put(
        self,
        session_id: str,
        turn_id: str,
        kind: KeyKind,
        key: str,
        entries: Sequence[AIAccessedFile],
    ) -> None:
        written_at = self._clock().timestamp()
        rows = [
            (session_id, turn_id, kind, key, entry.model_dump_json(), written_at)
            for entry in entries
        ]
        with self._write() as conn:
            conn.execute(
                "DELETE FROM codex_file_records WHERE session_id = ? AND key_kind = ? AND key = ?",
                (session_id, kind, key),
            )
            conn.executemany(
                "INSERT INTO codex_file_records"
                " (session_id, turn_id, key_kind, key, entry_json, written_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
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
        rows = (
            self._conn()
            .execute(
                "SELECT entry_json FROM codex_file_records"
                f" WHERE session_id = ? AND key_kind = ? AND key IN ({_placeholders(keys)})"
                " ORDER BY rowid",
                (session_id, kind, *keys),
            )
            .fetchall()
        )
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
        with self._write() as conn:
            for kind, keys in (("turn", turn_ids), ("call", tool_ids)):
                if keys:
                    conn.execute(
                        "DELETE FROM codex_file_records WHERE session_id = ? AND key_kind = ?"
                        f" AND key IN ({_placeholders(keys)})",
                        (session_id, kind, *keys),
                    )

    def delete_turn(self, session_id: str, turn_id: str) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM codex_file_records WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            )

    def delete_older_than(self, cutoff: datetime) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM codex_file_records WHERE written_at < ?", (cutoff.timestamp(),)
            )
