"""SQLite implementations of the checkpoint store and the tick lease (every
operation a single statement), the file-backed blob sink and the scheduler
check."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from .. import SchedulerAuth
from ..checkpoint import Checkpoint

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _to_us(when: datetime) -> int:
    """Microseconds since the epoch, exactly. A naive datetime is UTC."""
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    return (when - _EPOCH) // timedelta(microseconds=1)


def _from_us(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


class LocalCheckpointStore:
    """``CheckpointStore`` on one row per document."""

    def __init__(self, db: aiosqlite.Connection, *, collection: str, document: str) -> None:
        self._db = db
        self._key = (collection, document)

    async def load(self) -> Checkpoint:
        async with self._db.execute(
            "SELECT timestamp_us, id FROM checkpoints WHERE collection = ? AND document = ?",
            self._key,
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return Checkpoint(timestamp=None, id=None)
        us, id_ = row
        return Checkpoint(timestamp=None if us is None else _from_us(us), id=id_)

    async def save(self, checkpoint: Checkpoint) -> None:
        us = None if checkpoint.timestamp is None else _to_us(checkpoint.timestamp)
        await self._db.execute(
            "INSERT INTO checkpoints (collection, document, timestamp_us, id) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(collection, document) DO UPDATE"
            " SET timestamp_us = excluded.timestamp_us, id = excluded.id",
            (*self._key, us, checkpoint.id),
        )


class LocalTickLease:
    """``TickLease`` on one row: one atomic upsert takes it only when it is
    unheld, lapsed or released."""

    def __init__(self, db: aiosqlite.Connection, *, collection: str, document: str) -> None:
        self._db = db
        self._key = (collection, document)

    @contextlib.asynccontextmanager
    async def hold(self, lease: timedelta) -> AsyncIterator[bool]:
        owner = f"tick-{uuid.uuid4().hex[:8]}"
        held = await self.take(lease, owner=owner)
        if not held:
            log.info("%s skipped: another tick holds the lease", owner)
        try:
            yield held
        finally:
            if held:
                await self.release(owner=owner)

    async def take(self, lease: timedelta, *, owner: str, now: datetime | None = None) -> bool:
        """True when ``owner`` now holds it."""
        now = now or datetime.now(UTC)
        cursor = await self._db.execute(
            "INSERT INTO leases (collection, document, owner, expires_us) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(collection, document) DO UPDATE"
            " SET owner = excluded.owner, expires_us = excluded.expires_us"
            " WHERE leases.expires_us IS NULL OR leases.expires_us <= ?",
            (*self._key, owner, _to_us(now + lease), _to_us(now)),
        )
        return cursor.rowcount == 1

    async def release(self, *, owner: str) -> None:
        """A lease that lapsed and was taken by someone else is left alone:
        releasing it would give a running tick's guard away."""
        await self._db.execute(
            "UPDATE leases SET expires_us = NULL"
            " WHERE collection = ? AND document = ? AND owner = ?",
            (*self._key, owner),
        )


class LocalBlobSink:
    """``BlobSink`` as files under ``<root>/<bucket>/<name>``; ``get`` is for
    tests and tools. A file has nowhere to keep ``content_type``, so it is not
    kept."""

    def __init__(self, root: Path, *, bucket: str) -> None:
        if not bucket or bucket in (".", "..") or Path(bucket).name != bucket:
            raise ValueError(f"a bucket is one path segment, not {bucket!r}")
        self._dir = root / bucket

    def _path(self, name: str) -> Path:
        path = (self._dir / name).resolve()
        if not path.is_relative_to(self._dir.resolve()):
            raise ValueError(f"blob name {name!r} leaves its bucket")
        return path

    async def put(self, name: str, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(_write_atomically, self._path(name), data)

    async def get(self, name: str) -> bytes | None:
        return await asyncio.to_thread(_read, self._path(name))


def _write_atomically(path: Path, data: bytes) -> None:
    """Through a temporary file in the same directory, renamed into place, so
    a reader never sees half a blob."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def token_check(principal: str | None) -> SchedulerAuth:
    """The principal is the expected bearer token. Unset refuses everything."""

    async def check(token: str) -> bool:
        if not principal:
            log.error("no scheduler principal is configured; refusing every tick")
            return False
        return hmac.compare_digest(token.encode(), principal.encode())

    return check
