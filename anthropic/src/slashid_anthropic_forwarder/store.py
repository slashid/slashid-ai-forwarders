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

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.transforms import ArrayRemove, ArrayUnion

from .record import Append, PendingRecord, from_document


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
        # How long a tombstone suppresses a late reader's duplicate. It must
        # exceed JOIN_WAIT + POLL_LAG + one tick; the startup assertion that
        # enforces the inequality lands with the tick cadence in the deploy
        # chunk, and the default matches SLASHID_TOMBSTONE_TTL_SECONDS.
        tombstone_ttl: timedelta = timedelta(hours=2),
    ) -> None:
        self._client = client
        self._collection = client.collection(collection)
        self._join_wait = join_wait
        self._retry_backoff = retry_backoff
        self._tombstone_ttl = tombstone_ttl

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

    async def claim(
        self,
        address: str,
        lease: timedelta,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> PendingRecord | None:
        """Take the exclusive right to push, and return the record as it is.

        Compare-and-set on the snapshot's ``update_time``: any write between
        the read and the claim — another claimer, or a completer merging
        digests — invalidates the precondition and this caller backs off.
        Readiness is deliberately not checked here: the deadline sweep
        claims records that will never become ready, and flushing emits
        whatever the record holds.
        """
        now = now or datetime.now(UTC)
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("tombstoned_at") is not None:
            return None
        held = data.get("claim_expires_at")
        if held is not None and held > now:
            return None
        expires = now + lease
        try:
            await self._ref(address).update(
                {
                    "claim_owner": owner,
                    "claim_expires_at": expires,
                    # The lease IS the next-attempt time: a crash before
                    # `retire` leaves the record collectable at `expires`
                    # rather than orphaned.
                    "next_attempt_at": expires,
                },
                option=self._client.write_option(last_update_time=snapshot.update_time),
            )
        except (FailedPrecondition, NotFound):
            return None
        return from_document(
            address,
            {
                **data,
                "claim_owner": owner,
                "claim_expires_at": expires,
                "next_attempt_at": expires,
            },
        )

    async def due(self, now: datetime, limit: int) -> list[PendingRecord]:
        """Live records ready for a pusher, oldest first, bounded.

        One inequality, because ``next_attempt_at`` already folds in the
        lease and the backoff. The composite index this needs is
        ``tombstoned_at`` ASC, ``next_attempt_at`` ASC; the Terraform
        provisions it.
        """
        query = (
            self._collection.where(filter=FieldFilter("tombstoned_at", "==", None))
            .where(filter=FieldFilter("next_attempt_at", "<=", now))
            .order_by("next_attempt_at")
            .limit(limit)
        )
        return [
            from_document(snapshot.id, snapshot.to_dict() or {})
            async for snapshot in query.stream()
        ]

    async def retire(
        self, address: str, outcome: Retirement | str, *, now: datetime | None = None
    ) -> None:
        """Close a record out. ``FAILED`` is the only outcome that keeps it
        alive, and it releases the lease so a later tick can collect it."""
        now = now or datetime.now(UTC)
        if Retirement(outcome) is not Retirement.FAILED:
            # Push, then retire: a crash between them re-pushes an event the
            # terminal dedups, where the reverse order loses it outright.
            #
            # ``tombstone_expires_at`` is what the TTL policy keys on, and it
            # holds the expiry instant rather than the moment of tombstoning:
            # Firestore deletes once the nominated field is in the past, so a
            # policy pointed at ``tombstoned_at`` would collect every
            # tombstone as it was written. No live record carries the field,
            # so the policy cannot reach one.
            await self._ref(address).set(
                {
                    "tombstoned_at": now,
                    "tombstone_expires_at": now + self._tombstone_ttl,
                    "claim_owner": None,
                    "claim_expires_at": None,
                },
                merge=True,
            )
            return
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return
        attempts = int((snapshot.to_dict() or {}).get("attempts") or 0) + 1
        await self._ref(address).set(
            {
                "attempts": attempts,
                "claim_owner": None,
                "claim_expires_at": None,
                "next_attempt_at": now + self._retry_backoff * attempts,
            },
            merge=True,
        )

    async def seen(self, address: str) -> Seen:
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return Seen.ABSENT
        if (snapshot.to_dict() or {}).get("tombstoned_at") is not None:
            return Seen.TOMBSTONED
        return Seen.LIVE


class TickLease:
    """The guard one tick takes before it does any work.

    The flush needs no such thing — ``claim`` arbitrates per record — but
    the readers do: two concurrent ticks walk the same lagging window,
    spend the same rate limit twice, and both write a checkpoint that has
    no precondition, so the watermark can move backwards.

    One document, compare-and-set on its update time, exactly as ``claim``
    works. It lives in the pending collection and nothing there sees it:
    it carries no ``tombstoned_at``, so the IS_NULL filter behind ``due``
    skips it, and no ``tombstone_expires_at``, so the TTL policy never
    collects it.
    """

    def __init__(self, *, client: Any, collection: str, document: str = "tick") -> None:
        self._client = client
        self._ref = client.collection(collection).document(document)

    async def take(self, lease: timedelta, *, owner: str, now: datetime | None = None) -> bool:
        """True when this caller now holds it. False means another tick is
        running, which is not an error: the next cron fire picks the work
        up from the store, and Cloud Scheduler retries nothing."""
        now = now or datetime.now(UTC)
        snapshot = await self._ref.get()
        if not snapshot.exists:
            try:
                await self._ref.create({"tick_owner": owner, "tick_expires_at": now + lease})
            except AlreadyExists:
                return False
            return True
        held = (snapshot.to_dict() or {}).get("tick_expires_at")
        if held is not None and held > now:
            return False
        try:
            await self._ref.update(
                {"tick_owner": owner, "tick_expires_at": now + lease},
                option=self._client.write_option(last_update_time=snapshot.update_time),
            )
        except (FailedPrecondition, NotFound):
            return False
        return True

    async def release(self, *, owner: str) -> None:
        """Hand it back. A lease that lapsed and was taken by someone else
        is left alone: releasing it would give a running tick's guard away."""
        snapshot = await self._ref.get()
        if not snapshot.exists or (snapshot.to_dict() or {}).get("tick_owner") != owner:
            return
        with contextlib.suppress(FailedPrecondition, NotFound):
            await self._ref.update(
                {"tick_expires_at": None},
                option=self._client.write_option(last_update_time=snapshot.update_time),
            )


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
