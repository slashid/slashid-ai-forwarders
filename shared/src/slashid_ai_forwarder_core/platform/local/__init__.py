"""A platform on a local SQLite database, for development and tests.

Needs the ``[local]`` extra. ``create_local_platform`` opens the database and
closes it when the block ends; ``LocalPlatform`` wraps the open connection.
State lives in one directory: ``data.sqlite`` and ``blobs/``. With no
directory it lives in memory, and blobs in a temporary directory.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path

import aiosqlite
from platformdirs import user_data_dir

from .. import SchedulerAuth
from .database import DATABASE, MEMORY, open_database
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
async def create_local_platform(path: str | Path | None) -> AsyncIterator[LocalPlatform]:
    """``path`` is a directory, created if it is missing: the database is
    ``<path>/data.sqlite`` and blobs go under ``<path>/blobs/``. ``None`` keeps
    everything in memory, with blobs in a temporary directory removed when the
    block ends. Where the directory belongs is the caller's decision."""
    async with AsyncExitStack() as stack:
        if path is None:
            db = await open_database(MEMORY)
            blobs = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="slashid-blobs-")))
        else:
            root = Path(path)
            db = await open_database(root / DATABASE)
            blobs = root / "blobs"
        stack.push_async_callback(db.close)
        yield LocalPlatform(db, blobs=blobs)


class _Unset:
    """Tells a ``path`` that was left out from one that is ``None`` (memory)."""


_UNSET = _Unset()


def open_local_platform(
    *, app: str | None = None, path: str | Path | None | _Unset = _UNSET
) -> AbstractAsyncContextManager[LocalPlatform]:
    """What the registry's ``local`` resolves to. ``path`` is as
    ``create_local_platform`` takes it; left out, the directory is the user
    data directory for ``app`` (``~/.local/share/<app>`` on Linux). One of the
    two is required, so a platform never lands somewhere nobody chose."""
    if isinstance(path, _Unset):
        if app is None:
            raise TypeError("local needs `app`, which names its default directory, or `path`")
        path = user_data_dir(app, "slashid")
    return create_local_platform(path)
