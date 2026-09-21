"""The pending store: a port with six operations, and its Firestore adapter.

The receiver never pushes from the request path. It writes a record and
returns; a completing writer or the deadline sweep pushes later. Which
of the two gets to push is settled by ``claim``.

Everything backend-specific stays in the adapter: the document id (the
address — Firestore ids may not contain ``/``, which no address does),
the array transforms, the TTL policy — which keys on
``tombstone_expires_at``, a field only a tombstone carries — the named
database, and the composite index behind ``due`` (``tombstoned_at`` ASC,
``next_attempt_at`` ASC).
Another cloud reimplements six methods and nothing above this line
changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.transforms import ArrayRemove, ArrayUnion

from .record import Append, PendingRecord


class Seen(StrEnum):
    """Three states, and ``ABSENT`` is the one that matters: it is what lets
    a reader emit an invocation standalone."""

    LIVE = "live"
    TOMBSTONED = "tombstoned"
    ABSENT = "absent"


class Retirement(StrEnum):
    """``PUSHED`` tombstones; ``FAILED`` releases the claim and sets a
    next-attempt time; ``SUPERSEDED`` tombstones without pushing, which is
    how a successor frame discards a tail record."""

    PUSHED = "pushed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Outcome:
    """What a write did, and whether the record is now ready to push.

    ``stored`` is False when the address is tombstoned — or, for
    ``complete``, when no record exists — which is a no-op, not an error.
    """

    stored: bool
    ready: bool
    created: bool = False


NO_OP = Outcome(stored=False, ready=False)


class PendingStore(Protocol):
    """Six operations. The contracts are the design's, verbatim."""

    async def upsert(
        self,
        address: str,
        fields: dict[str, Any],
        expectations: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        """Create or merge. Creating sets the deadline and seeds
        expectations; merging never moves the deadline. A no-op on a
        tombstoned address, and it says so."""
        ...

    async def complete(
        self,
        address: str,
        fields: dict[str, Any],
        clears: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        """Merge and clear. Never creates. A no-op on a tombstoned address.
        Reports readiness for the same reason ``upsert`` does: a completing
        writer that sees it knows to claim."""
        ...

    async def claim(
        self,
        address: str,
        lease: timedelta,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> PendingRecord | None:
        """Take the exclusive right to push, for a bounded lease, and return
        the record **as it is now**. Every pusher calls it, the flusher
        included. ``None`` when someone else holds the lease, or the record
        is gone or tombstoned."""
        ...

    async def due(self, now: datetime, limit: int) -> list[PendingRecord]:
        """Live records past their deadline whose claim is absent or
        expired, oldest first, bounded."""
        ...

    async def retire(
        self, address: str, outcome: Retirement | str, *, now: datetime | None = None
    ) -> None: ...

    async def seen(self, address: str) -> Seen: ...


class FirestorePendingStore:
    """Firestore-backed ``PendingStore`` — one document per address."""

    def __init__(
        self,
        *,
        client: Any,  # google.cloud.firestore.AsyncClient — untyped as in vertex's store
        collection: str,
        join_wait: timedelta,
        retry_backoff: timedelta = timedelta(seconds=60),
    ) -> None:
        self._client = client
        self._collection = client.collection(collection)
        self._join_wait = join_wait
        self._retry_backoff = retry_backoff

    def _ref(self, address: str) -> Any:
        return self._collection.document(address)

    async def upsert(
        self,
        address: str,
        fields: dict[str, Any],
        expectations: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        now = now or datetime.now(UTC)
        deadline = now + self._join_wait
        try:
            await self._ref(address).create(
                {
                    "deadline": deadline,
                    # Equal on creation; the lease and the backoff move only
                    # this one, so the deadline the record was born with
                    # survives every merge.
                    "next_attempt_at": deadline,
                    "awaiting": list(expectations),
                    "attempts": 0,
                    "claim_owner": None,
                    "claim_expires_at": None,
                    # Written explicitly: an IS_NULL filter does not match a
                    # document that lacks the field, and `due` needs it to.
                    "tombstoned_at": None,
                    # Last, so the caller's fields win: ``event_fields`` sets
                    # ``elided`` only when it actually dropped text, and a
                    # literal after the spread would overwrite it on every
                    # create — which is every frame-built record.
                    **_plain(fields),
                }
            )
            return Outcome(stored=True, ready=not expectations, created=True)
        except AlreadyExists:
            pass
        snapshot = await self._ref(address).get()
        data = snapshot.to_dict() or {}
        if data.get("tombstoned_at") is not None:
            return NO_OP
        merge = _transforms(fields)
        if expectations:
            merge["awaiting"] = ArrayUnion(list(expectations))
        await self._ref(address).set(merge, merge=True)
        after = (await self._ref(address).get()).to_dict() or {}
        return Outcome(stored=True, ready=not after.get("awaiting"))

    async def complete(
        self,
        address: str,
        fields: dict[str, Any],
        clears: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return NO_OP
        if (snapshot.to_dict() or {}).get("tombstoned_at") is not None:
            return NO_OP
        merge = _transforms(fields)
        if clears:
            # ArrayRemove, not a rewrite: two readers clearing different
            # expectations must not clobber each other.
            merge["awaiting"] = ArrayRemove(list(clears))
        await self._ref(address).set(merge, merge=True)
        after = (await self._ref(address).get()).to_dict() or {}
        return Outcome(stored=True, ready=not after.get("awaiting"))

    async def seen(self, address: str) -> Seen:
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return Seen.ABSENT
        if (snapshot.to_dict() or {}).get("tombstoned_at") is not None:
            return Seen.TOMBSTONED
        return Seen.LIVE


def _transforms(fields: dict[str, Any]) -> dict[str, Any]:
    """Translate the record module's backend-neutral ``Append`` markers into
    Firestore array transforms."""
    return {
        key: ArrayUnion(list(value.values)) if isinstance(value, Append) else value
        for key, value in fields.items()
    }


def _plain(fields: dict[str, Any]) -> dict[str, Any]:
    """The same fields for a ``create``, where a transform is pointless (and
    an ``ArrayUnion`` against a field that does not exist yet is a write the
    backend has to resolve for nothing)."""
    return {
        key: list(value.values) if isinstance(value, Append) else value
        for key, value in fields.items()
    }
