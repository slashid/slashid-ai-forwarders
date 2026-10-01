"""What every platform's checkpoint store and tick lease must do, run against
the Firestore platform (on an in-memory fake) and the local one."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol, cast

import pytest

from fake_firestore import FakeFirestore
from slashid_ai_forwarder_core.platform import Checkpoint, Platform
from slashid_ai_forwarder_core.platform.gcp import GcpPlatform
from slashid_ai_forwarder_core.platform.local import create_local_platform

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
TICK = timedelta(minutes=10)


class _Lease(Protocol):
    """What both implementations offer beyond ``hold``: an explicit owner and
    clock, which is what lets these tests drive expiry."""

    def hold(self, lease: timedelta) -> AbstractAsyncContextManager[bool]: ...
    async def take(self, lease: timedelta, *, owner: str, now: datetime | None = None) -> bool: ...
    async def release(self, *, owner: str) -> None: ...


@pytest.fixture(params=["gcp", "local"])
async def platform(request: pytest.FixtureRequest) -> AsyncIterator[Platform]:
    if request.param == "gcp":
        built = GcpPlatform(project="p", firestore_database="d")
        built.__dict__["firestore"] = FakeFirestore()  # what the cached property would build
        yield built
    else:
        async with create_local_platform(None) as local:
            yield local


def _lease(platform: Platform, document: str = "tick") -> _Lease:
    return cast(_Lease, platform.tick_lease(collection="c", document=document))


# --- checkpoints -------------------------------------------------------------


async def test_a_document_never_saved_loads_empty(platform: Platform) -> None:
    assert await platform.checkpoint_store(collection="c", document="d").load() == Checkpoint(
        None, None
    )


async def test_a_saved_checkpoint_loads_back(platform: Platform) -> None:
    store = platform.checkpoint_store(collection="c", document="d")
    when = datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC)
    await store.save(Checkpoint(timestamp=when, id="42"))
    assert await store.load() == Checkpoint(timestamp=when, id="42")
    await store.save(Checkpoint(timestamp=when + timedelta(seconds=1), id=None))
    assert await store.load() == Checkpoint(timestamp=when + timedelta(seconds=1), id=None)


async def test_an_empty_checkpoint_round_trips_as_empty(platform: Platform) -> None:
    store = platform.checkpoint_store(collection="c", document="d")
    await store.save(Checkpoint(timestamp=datetime(2026, 9, 5, tzinfo=UTC), id="1"))
    await store.save(Checkpoint(None, None))
    assert await store.load() == Checkpoint(None, None)


async def test_a_naive_datetime_is_taken_as_utc(platform: Platform) -> None:
    store = platform.checkpoint_store(collection="c", document="d")
    await store.save(Checkpoint(timestamp=datetime(2026, 9, 5, 2, 43, 59), id="42"))
    loaded = await store.load()
    assert loaded.timestamp == datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC)


async def test_a_non_utc_datetime_loads_as_the_same_instant(platform: Platform) -> None:
    store = platform.checkpoint_store(collection="c", document="d")
    when = datetime(2026, 9, 5, 4, 43, 59, tzinfo=timezone(timedelta(hours=2)))
    await store.save(Checkpoint(timestamp=when, id="42"))
    assert (await store.load()).timestamp == when


async def test_documents_and_collections_are_separate(platform: Platform) -> None:
    a = platform.checkpoint_store(collection="c", document="a")
    b = platform.checkpoint_store(collection="c", document="b")
    other = platform.checkpoint_store(collection="other", document="a")
    await a.save(Checkpoint(timestamp=None, id="x"))
    assert await b.load() == Checkpoint(None, None)
    assert await other.load() == Checkpoint(None, None)
    assert (await a.load()).id == "x"


# --- the tick lease ----------------------------------------------------------


async def test_one_tick_takes_the_lease_and_the_next_is_turned_away(platform: Platform) -> None:
    first, second = _lease(platform), _lease(platform)
    assert await first.take(TICK, owner="tick-1", now=NOW) is True
    assert await second.take(TICK, owner="tick-2", now=NOW + timedelta(minutes=1)) is False


async def test_the_lease_is_free_again_after_its_owner_releases_it(platform: Platform) -> None:
    lease = _lease(platform)
    await lease.take(TICK, owner="tick-1", now=NOW)
    await lease.release(owner="tick-1")
    assert await _lease(platform).take(TICK, owner="tick-2", now=NOW + timedelta(minutes=1))


async def test_a_lease_nobody_released_lapses(platform: Platform) -> None:
    await _lease(platform).take(TICK, owner="crashed", now=NOW)
    later = NOW + TICK + timedelta(seconds=1)
    assert await _lease(platform).take(TICK, owner="next", now=later) is True


async def test_releasing_a_lease_someone_else_holds_does_nothing(platform: Platform) -> None:
    straggler = _lease(platform)
    await straggler.take(TICK, owner="crashed", now=NOW)
    await _lease(platform).take(TICK, owner="next", now=NOW + TICK + timedelta(seconds=1))
    await straggler.release(owner="crashed")
    third = await _lease(platform).take(TICK, owner="third", now=NOW + TICK + timedelta(minutes=1))
    assert third is False


async def test_leases_on_different_documents_do_not_contend(platform: Platform) -> None:
    assert await _lease(platform, "one").take(TICK, owner="a", now=NOW) is True
    assert await _lease(platform, "two").take(TICK, owner="b", now=NOW) is True


async def test_hold_hands_the_lease_back_when_the_block_ends(platform: Platform) -> None:
    async with _lease(platform).hold(TICK) as held:
        assert held is True
        async with _lease(platform).hold(TICK) as second:
            assert second is False
    async with _lease(platform).hold(TICK) as third:
        assert third is True


async def test_hold_hands_the_lease_back_when_the_block_raises(platform: Platform) -> None:
    with pytest.raises(RuntimeError):
        async with _lease(platform).hold(TICK):
            raise RuntimeError("the tick failed")
    async with _lease(platform).hold(TICK) as held:
        assert held is True


async def test_hold_that_was_turned_away_releases_nothing(platform: Platform) -> None:
    async with _lease(platform).hold(TICK):
        async with _lease(platform).hold(TICK) as second:
            assert second is False
        async with _lease(platform).hold(TICK) as third:
            assert third is False
