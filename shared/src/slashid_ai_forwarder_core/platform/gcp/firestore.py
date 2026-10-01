"""Firestore implementations of the checkpoint store and the tick lease."""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from ..checkpoint import Checkpoint

log = logging.getLogger(__name__)


class FirestoreCheckpointStore:
    """Firestore-backed ``CheckpointStore`` — one document per forwarder.

    Firestore Native mode is a singleton database per GCP project (until
    multi-database GA); ``vertex/``'s Terraform module provisions it
    conditionally via ``var.create_firestore_database``.
    """

    def __init__(
        self,
        *,
        client: Any,  # google.cloud.firestore.AsyncClient, untyped as in BqEventSource
        collection: str,
        document: str,
    ) -> None:
        self._doc_ref = client.collection(collection).document(document)

    async def load(self) -> Checkpoint:
        snap = await self._doc_ref.get()
        if not snap.exists:
            return Checkpoint(timestamp=None, id=None)
        data = snap.to_dict() or {}
        return _from_dict(data)

    async def save(self, checkpoint: Checkpoint) -> None:
        await self._doc_ref.set(_to_dict(checkpoint))


def _to_dict(checkpoint: Checkpoint) -> dict[str, Any]:
    # Firestore stores datetimes as UTC-normalized timestamps; ensure the
    # tz is set (BQ returns tz-aware ``datetime`` objects but be defensive
    # in case a caller synthesizes one for testing).
    if checkpoint.timestamp is None:
        return {"timestamp": None, "id": checkpoint.id}
    dt = checkpoint.timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return {"timestamp": dt, "id": checkpoint.id}


def _from_dict(data: dict[str, Any]) -> Checkpoint:
    ts = data.get("timestamp")
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return Checkpoint(
        timestamp=ts if isinstance(ts, datetime) else None,
        id=data.get("id"),
    )


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
