"""Polling-checkpoint persistence — protocol + Firestore-backed impl.

Firestore Native mode is a singleton database per GCP project (until
multi-database GA); the Terraform module provisions it conditionally
via ``var.create_firestore_database``. This store writes one document
holding the ``(timestamp, id)`` watermark.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from .event_source import Checkpoint


class CheckpointStore(Protocol):
    """Load/save the polling watermark.

    ``load`` returns an empty ``Checkpoint(None, None)`` on the very
    first tick (before any prior save) — the source then fetches every
    entry up to the batch bound.
    """

    def load(self) -> Checkpoint: ...
    def save(self, checkpoint: Checkpoint) -> None: ...


class FirestoreCheckpointStore:
    """Firestore-backed ``CheckpointStore`` — one document per forwarder."""

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
