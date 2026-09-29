"""The pending store: six operations, with one implementation per cloud
in this package (``gcp.FirestorePendingStore``).

The receiver never pushes from the request path. It writes a record and
returns; a completing writer or the deadline sweep pushes later. Which
of the two gets to push is settled by ``claim``.

Everything backend-specific stays in the implementation: document ids,
array transforms, the TTL policy and the indexes behind ``due`` and
``nearby``. Another cloud reimplements six methods and nothing here
changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from ..record import PendingRecord


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

    async def nearby(
        self,
        conversation_id: str,
        *,
        at: datetime,
        window: timedelta,
        limit: int = 25,
    ) -> list[PendingRecord]:
        """Live records of one conversation whose event timestamp is within
        ``window`` of ``at``. The soft join's candidate set, and the only
        read here that is not keyed on an address."""
        ...

    async def retire(
        self, address: str, outcome: Retirement | str, *, now: datetime | None = None
    ) -> None: ...

    async def seen(self, address: str) -> Seen: ...
