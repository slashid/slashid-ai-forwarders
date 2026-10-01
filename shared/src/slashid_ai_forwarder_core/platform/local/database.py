"""Opening the SQLite database behind ``LocalPlatform``."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

MEMORY = ":memory:"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS checkpoints ("
    " collection TEXT NOT NULL, document TEXT NOT NULL, timestamp_us INTEGER, id TEXT,"
    " PRIMARY KEY (collection, document))",
    "CREATE TABLE IF NOT EXISTS leases ("
    " collection TEXT NOT NULL, document TEXT NOT NULL, owner TEXT NOT NULL, expires_us INTEGER,"
    " PRIMARY KEY (collection, document))",
)


async def open_database(path: str | Path) -> aiosqlite.Connection:
    """A started connection with the tables in place.

    Autocommit (``isolation_level=None``): every statement commits by itself,
    so no implicit transaction is left open for a later one to trip over.
    """
    target = str(path)
    if target != MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = await aiosqlite.connect(target, isolation_level=None)
    try:
        if target != MEMORY:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
        for statement in _SCHEMA:
            await db.execute(statement)
    except BaseException:
        await db.close()
        raise
    return db
