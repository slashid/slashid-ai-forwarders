"""``PendingStore`` on SQLite: one row per address, written by compare-and-set.

The platform's connection is shared and autocommit, so no operation spans
statements. Each one reads a row and writes it back only if its ``version``
is unchanged, which is what Firestore's ``update_time`` precondition does,
and a lost race re-reads and tries again. ``doc`` holds the record as the
Firestore store would; the other columns copy what a query filters on.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from ..record import Append, PendingRecord, event_time, from_document
from . import NO_OP, Outcome, Retirement, Seen

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ATTEMPTS = 8
_DATETIMES = frozenset(
    {"deadline", "next_attempt_at", "claim_expires_at", "tombstoned_at", "tombstone_expires_at"}
)
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pending ("
    " collection TEXT NOT NULL, address TEXT NOT NULL, version INTEGER NOT NULL, doc TEXT NOT NULL,"
    " tombstoned_us INTEGER, next_attempt_us INTEGER, conversation_id TEXT,"
    " tombstone_expires_us INTEGER, PRIMARY KEY (collection, address))",
    "CREATE INDEX IF NOT EXISTS pending_due"
    " ON pending (collection, tombstoned_us, next_attempt_us)",
    "CREATE INDEX IF NOT EXISTS pending_conversation ON pending (collection, conversation_id)",
    "CREATE INDEX IF NOT EXISTS pending_expiry ON pending (collection, tombstone_expires_us)",
)
_COLUMNS = "tombstoned_us = ?, next_attempt_us = ?, conversation_id = ?, tombstone_expires_us = ?"


class WriteContention(RuntimeError):
    """A write lost its compare-and-set on every attempt."""


def _us(when: datetime) -> int:
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    return (when - _EPOCH) // timedelta(microseconds=1)


def _dump(doc: dict[str, Any]) -> str:
    return json.dumps({k: v.isoformat() if isinstance(v, datetime) else v for k, v in doc.items()})


def _load(text: str) -> dict[str, Any]:
    raw = json.loads(text)
    return {
        k: datetime.fromisoformat(v) if k in _DATETIMES and v is not None else v
        for k, v in raw.items()
    }


def _columns(doc: dict[str, Any]) -> tuple[int | None, int | None, str | None, int | None]:
    def us(key: str) -> int | None:
        value = doc.get(key)
        return None if value is None else _us(value)

    event = doc.get("event") or {}
    return (
        us("tombstoned_at"),
        us("next_attempt_at"),
        event.get("conversation_id"),
        us("tombstone_expires_at"),
    )


def _merge_map(base: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Firestore's ``set(merge=True)``: maps merge per field at every depth,
    lists and scalars replace, ``Append`` unions into a list."""
    out = dict(base)
    for key, value in fields.items():
        if isinstance(value, Append):
            current = list(out.get(key) or [])
            for v in value.values:
                if v not in current:
                    current.append(v)
            out[key] = current
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_map(out[key], value)
        else:
            out[key] = value
    return out


class SqlitePendingStore:
    """``PendingStore`` on the local platform's connection."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        collection: str,
        join_wait: timedelta,
        retry_backoff: timedelta = timedelta(seconds=60),
        tombstone_ttl: timedelta = timedelta(hours=2),
    ) -> None:
        self._db = db
        self._collection = collection
        self._join_wait = join_wait
        self._retry_backoff = retry_backoff
        self._tombstone_ttl = tombstone_ttl

    @classmethod
    async def open(cls, *, db: aiosqlite.Connection, **options: Any) -> SqlitePendingStore:
        """The store, with its table in place."""
        for statement in _SCHEMA:
            async with db.execute(statement):
                pass
        return cls(db=db, **options)

    async def _read(self, address: str) -> tuple[int, dict[str, Any]] | None:
        async with self._db.execute(
            "SELECT version, doc FROM pending WHERE collection = ? AND address = ?",
            (self._collection, address),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else (row[0], _load(row[1]))

    async def _insert(self, address: str, doc: dict[str, Any]) -> bool:
        async with self._db.execute(
            "INSERT INTO pending (collection, address, version, doc, tombstoned_us,"
            " next_attempt_us, conversation_id, tombstone_expires_us)"
            " VALUES (?, ?, 0, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (self._collection, address, _dump(doc), *_columns(doc)),
        ) as cursor:
            return cursor.rowcount == 1

    async def _write(self, address: str, version: int, doc: dict[str, Any]) -> bool:
        async with self._db.execute(
            f"UPDATE pending SET doc = ?, version = version + 1, {_COLUMNS}"
            " WHERE collection = ? AND address = ? AND version = ?",
            (_dump(doc), *_columns(doc), self._collection, address, version),
        ) as cursor:
            return cursor.rowcount == 1

    async def upsert(
        self,
        address: str,
        fields: dict[str, Any],
        expectations: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        now = now or datetime.now(UTC)
        for _ in range(_ATTEMPTS):
            row = await self._read(address)
            if row is None:
                deadline = now + self._join_wait
                base = {
                    "deadline": deadline,
                    "next_attempt_at": deadline,
                    "awaiting": list(expectations),
                    "attempts": 0,
                    "claim_owner": None,
                    "claim_expires_at": None,
                    "tombstoned_at": None,
                }
                if await self._insert(address, _merge_map(base, fields)):
                    return Outcome(stored=True, ready=not expectations, created=True)
                continue
            version, doc = row
            if doc.get("tombstoned_at") is not None:
                return NO_OP
            merged = _merge_map(doc, fields)
            if expectations:
                merged = _merge_map(merged, {"awaiting": Append(tuple(expectations))})
            if await self._write(address, version, merged):
                return Outcome(stored=True, ready=not merged.get("awaiting"))
        raise WriteContention(address)

    async def complete(
        self,
        address: str,
        fields: dict[str, Any],
        clears: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        for _ in range(_ATTEMPTS):
            row = await self._read(address)
            if row is None:
                return NO_OP
            version, doc = row
            if doc.get("tombstoned_at") is not None:
                return NO_OP
            merged = _merge_map(doc, fields)
            if clears:
                merged["awaiting"] = [a for a in merged.get("awaiting") or [] if a not in clears]
            if await self._write(address, version, merged):
                return Outcome(stored=True, ready=not merged.get("awaiting"))
        raise WriteContention(address)

    async def claim(
        self,
        address: str,
        lease: timedelta,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> PendingRecord | None:
        """One attempt: a lost compare-and-set means someone else holds it."""
        now = now or datetime.now(UTC)
        row = await self._read(address)
        if row is None:
            return None
        version, doc = row
        if doc.get("tombstoned_at") is not None:
            return None
        held = doc.get("claim_expires_at")
        if held is not None and held > now:
            return None
        expires = now + lease
        claimed = {
            **doc,
            "claim_owner": owner,
            "claim_expires_at": expires,
            "next_attempt_at": expires,
        }
        if not await self._write(address, version, claimed):
            return None
        return from_document(address, claimed)

    async def due(self, now: datetime, limit: int) -> list[PendingRecord]:
        """Also where expired tombstones are deleted: SQLite has no TTL
        policy, and this runs once per tick."""
        async with self._db.execute(
            "DELETE FROM pending WHERE collection = ? AND tombstone_expires_us <= ?",
            (self._collection, _us(now)),
        ):
            pass
        async with self._db.execute(
            "SELECT address, doc FROM pending WHERE collection = ? AND tombstoned_us IS NULL"
            " AND next_attempt_us <= ? ORDER BY next_attempt_us, address LIMIT ?",
            (self._collection, _us(now), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [from_document(address, _load(doc)) for address, doc in rows]

    async def nearby(
        self,
        conversation_id: str,
        *,
        at: datetime,
        window: timedelta,
        limit: int = 25,
    ) -> list[PendingRecord]:
        async with self._db.execute(
            "SELECT address, doc FROM pending WHERE collection = ? AND tombstoned_us IS NULL"
            " AND conversation_id = ? LIMIT ?",
            (self._collection, conversation_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        out: list[PendingRecord] = []
        for address, doc in rows:
            record = from_document(address, _load(doc))
            when = event_time(record)
            if when is not None and abs(when - at) <= window:
                out.append(record)
        return out

    async def retire(
        self, address: str, outcome: Retirement | str, *, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(UTC)
        failed = Retirement(outcome) is Retirement.FAILED
        for _ in range(_ATTEMPTS):
            row = await self._read(address)
            if row is None:
                if failed:
                    return
                if await self._insert(address, self._tombstone(now)):
                    return
                continue
            version, doc = row
            if failed:
                attempts = int(doc.get("attempts") or 0) + 1
                doc = {
                    **doc,
                    "attempts": attempts,
                    "claim_owner": None,
                    "claim_expires_at": None,
                    "next_attempt_at": now + self._retry_backoff * attempts,
                }
            else:
                doc = {**doc, **self._tombstone(now)}
            if await self._write(address, version, doc):
                return
        raise WriteContention(address)

    def _tombstone(self, now: datetime) -> dict[str, Any]:
        return {
            "tombstoned_at": now,
            "tombstone_expires_at": now + self._tombstone_ttl,
            "claim_owner": None,
            "claim_expires_at": None,
        }

    async def seen(self, address: str) -> Seen:
        row = await self._read(address)
        if row is None:
            return Seen.ABSENT
        return Seen.TOMBSTONED if row[1].get("tombstoned_at") is not None else Seen.LIVE
