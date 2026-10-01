"""``FirestoreTickLease``: one tick at a time, and a dead one costs a cycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from google.cloud.firestore import AsyncClient as FirestoreAsyncClient

from fake_firestore import FakeFirestore
from slashid_ai_forwarder_core.platform.gcp.firestore import FirestoreTickLease

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
TICK = timedelta(minutes=10)


def a_lease(db: FakeFirestore | None = None) -> tuple[FirestoreTickLease, FakeFirestore]:
    db = db or FakeFirestore()
    return FirestoreTickLease(
        client=cast(FirestoreAsyncClient, db), collection="c", document="tick"
    ), db


async def test_one_tick_takes_the_lease_and_the_next_is_turned_away() -> None:
    lease, db = a_lease()
    assert await lease.take(TICK, owner="tick-1", now=NOW) is True
    second, _ = a_lease(db)
    assert await second.take(TICK, owner="tick-2", now=NOW + timedelta(minutes=1)) is False


async def test_the_lease_is_released_at_the_end_of_a_tick() -> None:
    lease, db = a_lease()
    await lease.take(TICK, owner="tick-1", now=NOW)
    await lease.release(owner="tick-1")
    second, _ = a_lease(db)
    assert await second.take(TICK, owner="tick-2", now=NOW + timedelta(minutes=1)) is True


async def test_a_lease_nobody_released_lapses() -> None:
    """A tick that died mid-pass costs one cycle, not the service."""
    lease, db = a_lease()
    await lease.take(TICK, owner="crashed", now=NOW)
    second, _ = a_lease(db)
    assert await second.take(TICK, owner="next", now=NOW + TICK + timedelta(seconds=1)) is True


async def test_releasing_a_lease_someone_else_holds_does_nothing() -> None:
    """After a lapse the lease belongs to the next tick; a straggler
    finishing its own pass must not hand it away."""
    lease, db = a_lease()
    await lease.take(TICK, owner="crashed", now=NOW)
    second, _ = a_lease(db)
    await second.take(TICK, owner="next", now=NOW + TICK + timedelta(seconds=1))
    await lease.release(owner="crashed")
    third, _ = a_lease(db)
    assert await third.take(TICK, owner="third", now=NOW + TICK + timedelta(minutes=1)) is False


async def test_hold_hands_the_lease_back_when_the_block_ends() -> None:
    lease, db = a_lease()
    async with lease.hold(TICK) as held:
        assert held is True
        async with a_lease(db)[0].hold(TICK) as second:
            assert second is False
    async with a_lease(db)[0].hold(TICK) as third:
        assert third is True


async def test_hold_hands_the_lease_back_when_the_block_raises() -> None:
    lease, db = a_lease()
    with pytest.raises(RuntimeError):
        async with lease.hold(TICK):
            raise RuntimeError("the tick failed")
    async with a_lease(db)[0].hold(TICK) as held:
        assert held is True


async def test_hold_that_was_turned_away_releases_nothing() -> None:
    """Leaving the block without the lease must not free the holder's."""
    lease, db = a_lease()
    async with lease.hold(TICK):
        async with a_lease(db)[0].hold(TICK) as second:
            assert second is False
        async with a_lease(db)[0].hold(TICK) as third:
            assert third is False
