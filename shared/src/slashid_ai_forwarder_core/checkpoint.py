"""Polling-checkpoint persistence — the watermark, its port, its adapter.

Shared because three feeds in ``anthropic/`` need exactly what ``vertex/``
already had. The type and its store live in one module so that neither
imports the other: they used to sit either side of a cycle, with the
dataclass in ``event_source.py`` and the protocol importing it back under
``TYPE_CHECKING``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Checkpoint:
    """The polling watermark — ``(timestamp, id)`` of the last processed
    entry. Universal across event sources.

    ``timestamp = None`` means "no entries seen yet". What a source does
    with that is the source's decision, and the two in this repo differ:
    Vertex fetches every entry up to its batch bound, while the Anthropic
    compliance readers start at ``now - POLL_LAG`` instead, because a
    backfill there would re-emit the whole retention window.

    Always a timestamp, **never a feed's page token**: those are
    documented as format-unstable, and they paginate within one tick and
    are then discarded.
    """

    timestamp: datetime | None
    id: str | None


class CheckpointStore(Protocol):
    """Load/save the polling watermark.

    ``load`` returns an empty ``Checkpoint(None, None)`` on the very
    first tick (before any prior save) — the source then fetches every
    entry up to the batch bound.
    """

    def load(self) -> Checkpoint: ...
    def save(self, checkpoint: Checkpoint) -> None: ...


class FirestoreCheckpointStore:
    """Firestore-backed ``CheckpointStore`` — one document per forwarder.

    Firestore Native mode is a singleton database per GCP project (until
    multi-database GA); ``vertex/``'s Terraform module provisions it
    conditionally via ``var.create_firestore_database``.
    """

    def __init__(
        self,
        *,
        client: Any,  # google.cloud.firestore.Client — untyped for the same reason as BqEventSource
        collection: str,
        document: str,
    ) -> None:
        self._doc_ref = client.collection(collection).document(document)

    def load(self) -> Checkpoint:
        snap = self._doc_ref.get()
        if not snap.exists:
            return Checkpoint(timestamp=None, id=None)
        data = snap.to_dict() or {}
        return _from_dict(data)

    def save(self, checkpoint: Checkpoint) -> None:
        self._doc_ref.set(_to_dict(checkpoint))


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
