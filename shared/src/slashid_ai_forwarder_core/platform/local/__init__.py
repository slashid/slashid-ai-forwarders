"""A platform on a local SQLite database, for development and tests.

Needs the ``[local]`` extra. ``create_local_platform`` opens the database and
closes it when the block ends; ``LocalPlatform`` wraps the open connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .. import SchedulerAuth
from .database import open_database
from .stores import LocalBlobSink, LocalCheckpointStore, LocalTickLease, token_check


class LocalPlatform:
    """One connection serves every store: an in-memory database exists per
    connection, so sharing is what makes ``:memory:`` work. The connection
    is exposed, as ``GcpPlatform.firestore`` is, so an adapter with state of
    its own can share it; its lifetime belongs to ``create_local_platform``."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @property
    def sqlite(self) -> aiosqlite.Connection:
        return self._db

    def checkpoint_store(self, *, collection: str, document: str) -> LocalCheckpointStore:
        return LocalCheckpointStore(self._db, collection=collection, document=document)

    def tick_lease(self, *, collection: str, document: str) -> LocalTickLease:
        return LocalTickLease(self._db, collection=collection, document=document)

    def blob_sink(self, bucket: str) -> LocalBlobSink:
        return LocalBlobSink(self._db, bucket=bucket)

    def scheduler_auth(self, *, principal: str | None, audience: str | None) -> SchedulerAuth:
        """``principal`` is the expected bearer token; there is no token to
        carry an audience, so ``audience`` is ignored."""
        return token_check(principal)


@asynccontextmanager
async def create_local_platform(path: str | Path) -> AsyncIterator[LocalPlatform]:
    """``path`` is a file, or ``":memory:"``; where a file belongs is the
    caller's decision."""
    db = await open_database(path)
    try:
        yield LocalPlatform(db)
    finally:
        await db.close()
