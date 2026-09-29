"""The guard one tick takes before it does any work — its port and its
Firestore adapter, side by side as in ``checkpoint.py``.

A scheduler can start a second tick while the first is still running:
Cloud Run hands a concurrent request to a second instance, and nothing in
Cloud Scheduler serializes them. Two readers walking the same window
spend the same rate limit twice and both write a checkpoint that has no
precondition, so the watermark can move backwards. The lease is what
makes an overlapping tick a no-op instead.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

log = logging.getLogger(__name__)


class TickLease(Protocol):
    def hold(self, lease: timedelta) -> AbstractAsyncContextManager[bool]:
        """``async with lease.hold(duration) as held:``. ``held`` is False
        when another tick is running, which is not an error: the body should
        skip, and the next scheduled tick picks the work up. When True, the
        lease is handed back on every way out of the block."""
        ...


class FirestoreTickLease:
    """``TickLease`` on one Firestore document, compare-and-set on its update
    time. It may share a collection with other documents, as long as no
    query there matches ``tick_owner`` / ``tick_expires_at``."""

    def __init__(self, *, client: Any, collection: str, document: str = "tick") -> None:
        self._client = client  # google.cloud.firestore.AsyncClient
        self._ref = client.collection(collection).document(document)

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
        from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound

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
        """A lease that lapsed and was taken by someone else is left alone:
        releasing it would give a running tick's guard away."""
        from google.api_core.exceptions import FailedPrecondition, NotFound

        snapshot = await self._ref.get()
        if not snapshot.exists or (snapshot.to_dict() or {}).get("tick_owner") != owner:
            return
        with contextlib.suppress(FailedPrecondition, NotFound):
            await self._ref.update(
                {"tick_expires_at": None},
                option=self._client.write_option(last_update_time=snapshot.update_time),
            )
