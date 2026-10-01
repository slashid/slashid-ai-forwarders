"""``DevPlatform``: a stand-in for ``LocalPlatform`` (contract in the Codex
hooks spec), removed when it lands. Not for production."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from slashid_ai_forwarder_core.platform import Checkpoint

BUSY_TIMEOUT_MS = 5_000


def _utc_text(ts: datetime) -> str:
    """Fixed width, so stored timestamps compare as text."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _read_created_at(path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


class DevPlatform:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._db = state_dir / "state.sqlite3"

    def _ensure_dir(self) -> None:
        try:
            self._state_dir.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            return
        os.chmod(self._state_dir, 0o700)

    def connect(self) -> sqlite3.Connection:
        self._ensure_dir()
        if not self._db.exists():
            with contextlib.suppress(FileExistsError):
                os.close(os.open(self._db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        conn = sqlite3.connect(self._db, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def created_at(self) -> datetime:
        """The first call's time, persisted in ``state_dir/created_at``; an
        empty or garbled file counts as absent."""
        self._ensure_dir()
        path = self._state_dir / "created_at"
        if (existing := _read_created_at(path)) is not None:
            return existing
        fd, tmp = tempfile.mkstemp(dir=self._state_dir, prefix=".created_at.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_utc_text(datetime.now(UTC)))
            try:
                os.link(tmp, path)
            except FileExistsError:
                if _read_created_at(path) is None:
                    os.replace(tmp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        created = _read_created_at(path)
        if created is None:
            raise RuntimeError(f"cannot read {path}")
        return created

    def checkpoint_store(self, *, collection: str, document: str) -> _CheckpointStore:
        return _CheckpointStore(self, collection, document)


class _CheckpointStore:
    def __init__(self, platform: DevPlatform, collection: str, document: str) -> None:
        self._platform = platform
        self._key = (collection, document)

    def _connect(self) -> sqlite3.Connection:
        conn = self._platform.connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dev_checkpoints ("
            " collection TEXT NOT NULL, document TEXT NOT NULL, timestamp TEXT, id TEXT,"
            " PRIMARY KEY (collection, document))"
        )
        return conn

    def load(self) -> Checkpoint:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT timestamp, id FROM dev_checkpoints WHERE collection = ? AND document = ?",
                self._key,
            ).fetchone()
        if row is None:
            return Checkpoint(None, None)
        timestamp, id_ = row
        return Checkpoint(datetime.fromisoformat(timestamp) if timestamp else None, id_)

    def save(self, checkpoint: Checkpoint) -> None:
        """Writes only a timestamp at or after the stored one."""
        timestamp = _utc_text(checkpoint.timestamp) if checkpoint.timestamp else None
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO dev_checkpoints (collection, document, timestamp, id)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (collection, document) DO UPDATE"
                " SET timestamp = excluded.timestamp, id = excluded.id"
                " WHERE dev_checkpoints.timestamp IS NULL"
                " OR excluded.timestamp >= dev_checkpoints.timestamp",
                (*self._key, timestamp, checkpoint.id),
            )
