"""Tests for ``FirestoreCheckpointStore`` — load/save the polling watermark.

Uses a fake ``firestore.Client`` triple that stores documents in an
in-memory dict. Firestore emulator is available but overkill for the
narrow load/save surface this store exposes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from slashid_vertex_forwarder.checkpoint_store import (
    Checkpoint,
    FirestoreCheckpointStore,
)


class _FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict[str, Any] | None:
        return self._data


class _FakeDocRef:
    def __init__(self, backing: dict[str, dict[str, Any]], path: str) -> None:
        self._backing = backing
        self._path = path

    def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self._backing.get(self._path))

    def set(self, data: dict[str, Any]) -> None:
        self._backing[self._path] = data


class _FakeCollectionRef:
    def __init__(self, backing: dict[str, dict[str, Any]], collection: str) -> None:
        self._backing = backing
        self._collection = collection

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._backing, f"{self._collection}/{doc_id}")


class _FakeFirestoreClient:
    def __init__(self) -> None:
        self.storage: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> _FakeCollectionRef:
        return _FakeCollectionRef(self.storage, name)


def _store() -> tuple[FirestoreCheckpointStore, _FakeFirestoreClient]:
    client = _FakeFirestoreClient()
    return (
        FirestoreCheckpointStore(
            client=client,
            collection="slashid_vertex",
            document="checkpoint",
        ),
        client,
    )


def test_load_returns_empty_when_document_absent() -> None:
    """First tick: no prior save → empty checkpoint (both fields None)."""
    store, _ = _store()
    cp = store.load()
    assert cp == Checkpoint(None, None)


def test_save_and_load_round_trip() -> None:
    store, _ = _store()
    when = datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC)
    store.save(Checkpoint(last_logging_time=when, last_request_id="42"))
    cp = store.load()
    assert cp == Checkpoint(last_logging_time=when, last_request_id="42")


def test_save_normalizes_naive_datetime_to_utc() -> None:
    """A caller-synthesized naive datetime gets tz-normalized on save so
    the load-side comparison against tz-aware BQ timestamps works."""
    store, client = _store()
    naive = datetime(2026, 9, 5, 2, 43, 59)
    store.save(Checkpoint(last_logging_time=naive, last_request_id="42"))
    stored = client.storage["slashid_vertex/checkpoint"]["last_logging_time"]
    assert stored.tzinfo is not None


def test_save_persists_to_correct_document_path() -> None:
    store, client = _store()
    store.save(Checkpoint(last_logging_time=datetime(2026, 9, 5, tzinfo=UTC), last_request_id="1"))
    assert "slashid_vertex/checkpoint" in client.storage


def test_load_normalizes_naive_stored_datetime() -> None:
    """Defence in depth: if a legacy or hand-edited document holds a
    naive timestamp, load() normalizes to UTC before returning."""
    store, client = _store()
    naive = datetime(2026, 9, 5, 2, 43, 59)
    client.storage["slashid_vertex/checkpoint"] = {
        "last_logging_time": naive,
        "last_request_id": "42",
    }
    cp = store.load()
    assert cp.last_logging_time is not None
    assert cp.last_logging_time.tzinfo is not None


def test_save_null_checkpoint_round_trips_as_empty() -> None:
    """Empty (None, None) save is legal — matches the pre-first-tick state."""
    store, _ = _store()
    store.save(Checkpoint(None, None))
    cp = store.load()
    assert cp == Checkpoint(None, None)
