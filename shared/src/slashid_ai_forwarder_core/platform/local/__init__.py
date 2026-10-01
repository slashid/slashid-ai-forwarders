"""A platform on a local SQLite database, for development and tests.

Needs the ``[local]`` extra. ``create_local_platform`` opens the database and
closes it when the block ends; ``LocalPlatform`` wraps the open connection.
Blobs are files: beside a database file, or in a temporary directory for
``:memory:``.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import aiosqlite

from .. import SchedulerAuth
from .database import MEMORY, open_database
from .stores import LocalBlobSink, LocalCheckpointStore, LocalTickLease, token_check


class LocalPlatform:
    """One connection serves every store: an in-memory database exists per
    connection, so sharing is what makes ``:memory:`` work. The connection
    is exposed, as ``GcpPlatform.firestore`` is, so an adapter with state of
    its own can share it; its lifetime belongs to ``create_local_platform``."""

    def __init__(self, db: aiosqlite.Connection, *, blobs: Path) -> None:
        self._db = db
        self._blobs = blobs

    @property
    def sqlite(self) -> aiosqlite.Connection:
        return self._db

    @property
    def blobs(self) -> Path:
        """The directory blobs are written under."""
        return self._blobs

    def checkpoint_store(self, *, collection: str, document: str) -> LocalCheckpointStore:
        return LocalCheckpointStore(self._db, collection=collection, document=document)

    def tick_lease(self, *, collection: str, document: str) -> LocalTickLease:
        return LocalTickLease(self._db, collection=collection, document=document)

    def blob_sink(self, bucket: str) -> LocalBlobSink:
        return LocalBlobSink(self._blobs, bucket=bucket)

    def scheduler_auth(self, *, principal: str | None, audience: str | None) -> SchedulerAuth:
        """``principal`` is the expected bearer token; there is no token to
        carry an audience, so ``audience`` is ignored."""
        return token_check(principal)


@asynccontextmanager
async def create_local_platform(path: str | Path) -> AsyncIterator[LocalPlatform]:
    """``path`` is a file, or ``":memory:"``; where a file belongs is the
    caller's decision. Blobs go in ``<path>.blobs/``, or for ``:memory:`` in a
    temporary directory removed when the block ends."""
    async with AsyncExitStack() as stack:
        db = await open_database(path)
        stack.push_async_callback(db.close)
        if str(path) == MEMORY:
            blobs = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="slashid-blobs-")))
        else:
            blobs = Path(f"{path}.blobs")
        yield LocalPlatform(db, blobs=blobs)
