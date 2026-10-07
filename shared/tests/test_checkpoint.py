"""Tests for ``FirestoreCheckpointStore`` — load/save the polling watermark.

Uses the shared in-memory ``firestore.AsyncClient`` fake that stores documents in an
dict. Firestore emulator is available but overkill for the
narrow load/save surface this store exposes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from google.cloud.firestore import AsyncClient as FirestoreAsyncClient

from fake_firestore import FakeFirestore
from slashid_ai_forwarder_core.platform import Checkpoint, load_or_start
from slashid_ai_forwarder_core.platform.gcp.firestore import FirestoreCheckpointStore


def _store() -> tuple[FirestoreCheckpointStore, FakeFirestore]:
    client = FakeFirestore()
    return (
        FirestoreCheckpointStore(
            client=cast(FirestoreAsyncClient, client),
            collection="slashid_vertex",
            document="checkpoint",
        ),
        client,
    )


async def test_load_returns_empty_when_document_absent() -> None:
    """First tick: no prior save → empty checkpoint (both fields None)."""
    store, _ = _store()
    cp = await store.load()
    assert cp == Checkpoint(None, None)


async def test_save_and_load_round_trip() -> None:
    store, _ = _store()
    when = datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC)
    await store.save(Checkpoint(timestamp=when, id="42"))
    cp = await store.load()
    assert cp == Checkpoint(timestamp=when, id="42")


async def test_save_normalizes_naive_datetime_to_utc() -> None:
    """A caller-synthesized naive datetime gets tz-normalized on save so
    the load-side comparison against tz-aware BQ timestamps works."""
    store, client = _store()
    naive = datetime(2026, 9, 5, 2, 43, 59)
    await store.save(Checkpoint(timestamp=naive, id="42"))
    stored = client.docs["slashid_vertex/checkpoint"][0]["timestamp"]
    assert stored.tzinfo is not None


async def test_save_persists_to_correct_document_path() -> None:
    store, client = _store()
    await store.save(Checkpoint(timestamp=datetime(2026, 9, 5, tzinfo=UTC), id="1"))
    assert "slashid_vertex/checkpoint" in client.docs


async def test_load_normalizes_naive_stored_datetime() -> None:
    """Defence in depth: if a legacy or hand-edited document holds a
    naive timestamp, load() normalizes to UTC before returning."""
    store, client = _store()
    naive = datetime(2026, 9, 5, 2, 43, 59)
    client.write("slashid_vertex/checkpoint", {"timestamp": naive, "id": "42"})
    cp = await store.load()
    assert cp.timestamp is not None
    assert cp.timestamp.tzinfo is not None


async def test_save_null_checkpoint_round_trips_as_empty() -> None:
    """Empty (None, None) save is legal — matches the pre-first-tick state."""
    store, _ = _store()
    await store.save(Checkpoint(None, None))
    cp = await store.load()
    assert cp == Checkpoint(None, None)


class _Memory:
    def __init__(self, value: Checkpoint) -> None:
        self.value = value
        self.saves: list[Checkpoint] = []

    async def load(self) -> Checkpoint:
        return self.value

    async def save(self, checkpoint: Checkpoint) -> None:
        self.value = checkpoint
        self.saves.append(checkpoint)


async def test_load_or_start_pins_and_saves_a_cold_start() -> None:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    store = _Memory(Checkpoint(None, None))
    assert await load_or_start(store, now=now, id="") == Checkpoint(now, "")
    assert store.saves == [Checkpoint(now, "")]
    later = datetime(2026, 10, 8, tzinfo=UTC)
    assert await load_or_start(store, now=later, id="") == Checkpoint(now, "")
    assert len(store.saves) == 1


async def test_load_or_start_resumes_a_saved_checkpoint_untouched() -> None:
    saved = Checkpoint(datetime(2026, 10, 1, tzinfo=UTC), "x")
    store = _Memory(saved)
    assert await load_or_start(store, now=datetime(2026, 10, 7, tzinfo=UTC)) == saved
    assert store.saves == []
