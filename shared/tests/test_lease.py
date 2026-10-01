"""``FirestoreTickLease``: one tick at a time, and a dead one costs a cycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound
from google.cloud.firestore import AsyncClient as FirestoreAsyncClient

from slashid_ai_forwarder_core.platform.gcp.firestore import FirestoreTickLease

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
TICK = timedelta(minutes=10)


class _Option:
    def __init__(self, last_update_time: int) -> None:
        self.last_update_time = last_update_time


class _Doc:
    """``update`` honours the precondition, which is the whole of the lease."""

    def __init__(self, db: _Firestore, key: str) -> None:
        self._db, self._key = db, key

    async def get(self) -> Any:
        held = self._db.docs.get(self._key)
        data, version = held if held else (None, None)
        fields = {"exists": held is not None, "update_time": version, "to_dict": lambda _: data}
        return type("Snap", (), fields)()

    async def create(self, data: dict[str, Any]) -> None:
        if self._key in self._db.docs:
            raise AlreadyExists(self._key)
        self._db.write(self._key, dict(data))

    async def update(self, data: dict[str, Any], option: _Option) -> None:
        held = self._db.docs.get(self._key)
        if held is None:
            raise NotFound(self._key)
        if option.last_update_time != held[1]:
            raise FailedPrecondition(self._key)
        self._db.write(self._key, {**held[0], **data})


class _Firestore:
    def __init__(self) -> None:
        self.docs: dict[str, tuple[dict[str, Any], int]] = {}
        self._clock = 0

    def write(self, key: str, data: dict[str, Any]) -> None:
        self._clock += 1
        self.docs[key] = (data, self._clock)

    def collection(self, name: str) -> Any:
        return type("Col", (), {"document": lambda _, doc: _Doc(self, f"{name}/{doc}")})()

    @staticmethod
    def write_option(*, last_update_time: int) -> _Option:
        return _Option(last_update_time)


def a_lease(db: _Firestore | None = None) -> tuple[FirestoreTickLease, _Firestore]:
    db = db or _Firestore()
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
