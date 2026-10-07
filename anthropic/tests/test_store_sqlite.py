"""``SqlitePendingStore`` behaviour with no Firestore counterpart."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from slashid_ai_forwarder_core.platform.local import LocalPlatform, create_local_platform

from slashid_anthropic_forwarder.record import Append
from slashid_anthropic_forwarder.store import Retirement, Seen
from slashid_anthropic_forwarder.store.local import SqlitePendingStore, WriteContention

NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)
JOIN_WAIT = timedelta(hours=1)
LEASE = timedelta(minutes=5)
TTL = timedelta(hours=2)
PAST_DEADLINE = NOW + JOIN_WAIT + timedelta(minutes=1)
ADDRESS = "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


async def open_store(platform: LocalPlatform) -> SqlitePendingStore:
    return await SqlitePendingStore.open(
        db=platform.sqlite, collection="anthropic_pending", join_wait=JOIN_WAIT, tombstone_ttl=TTL
    )


@pytest.fixture
async def platform() -> AsyncIterator[LocalPlatform]:
    async with create_local_platform(None) as opened:
        yield opened


@pytest.fixture
async def store(platform: LocalPlatform) -> SqlitePendingStore:
    return await open_store(platform)


async def test_a_claim_that_loses_the_compare_and_set_returns_none(
    store: SqlitePendingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.upsert(ADDRESS, {"event": {}}, (), now=NOW)
    read = store._read

    async def rival_claims_after_the_read(address: str):
        row = await read(address)
        monkeypatch.setattr(store, "_read", read)
        assert await store.claim(address, LEASE, owner="rival", now=PAST_DEADLINE) is not None
        return row

    monkeypatch.setattr(store, "_read", rival_claims_after_the_read)
    assert await store.claim(ADDRESS, LEASE, owner="loser", now=PAST_DEADLINE) is None
    record = await store.claim(ADDRESS, LEASE, owner="late", now=PAST_DEADLINE + LEASE * 2)
    assert record is not None


async def test_an_append_dedups_its_own_repeated_values(store: SqlitePendingStore) -> None:
    await store.upsert(ADDRESS, {"webhook_ids": Append(("x", "x"))}, (), now=NOW)
    record = await store.claim(ADDRESS, LEASE, owner="t", now=PAST_DEADLINE)
    assert record is not None
    assert record.webhook_ids == ["x"]


async def test_two_interleaved_upserts_both_extend_one_field(store: SqlitePendingStore) -> None:
    await store.upsert(ADDRESS, {"event": {}}, (), now=NOW)
    await asyncio.gather(
        store.upsert(ADDRESS, {"webhook_ids": Append(("a",))}, (), now=NOW),
        store.upsert(ADDRESS, {"webhook_ids": Append(("b",))}, (), now=NOW),
    )
    record = await store.claim(ADDRESS, LEASE, owner="t", now=PAST_DEADLINE)
    assert record is not None and sorted(record.webhook_ids) == ["a", "b"]


async def test_two_interleaved_creates_leave_one_record_with_both_values(
    store: SqlitePendingStore,
) -> None:
    outcomes = await asyncio.gather(
        store.upsert(ADDRESS, {"webhook_ids": Append(("a",))}, (), now=NOW),
        store.upsert(ADDRESS, {"webhook_ids": Append(("b",))}, (), now=NOW),
    )
    assert sorted(o.created for o in outcomes) == [False, True]
    record = await store.claim(ADDRESS, LEASE, owner="t", now=PAST_DEADLINE)
    assert record is not None and sorted(record.webhook_ids) == ["a", "b"]


async def test_a_write_that_never_wins_raises(
    store: SqlitePendingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.upsert(ADDRESS, {"event": {}}, (), now=NOW)

    async def always_lost(*_: object) -> bool:
        return False

    monkeypatch.setattr(store, "_write", always_lost)
    with pytest.raises(WriteContention):
        await store.upsert(ADDRESS, {"webhook_ids": Append(("a",))}, (), now=NOW)


async def test_due_deletes_expired_tombstones_and_keeps_live_ones(
    store: SqlitePendingStore,
) -> None:
    await store.upsert("toolu_old", {"event": {}}, (), now=NOW)
    await store.upsert("toolu_new", {"event": {}}, (), now=NOW)
    await store.retire("toolu_old", Retirement.PUSHED, now=NOW)
    await store.retire("toolu_new", Retirement.PUSHED, now=NOW + timedelta(hours=1))
    await store.due(NOW + TTL - timedelta(seconds=1), 10)
    assert await store.seen("toolu_old") is Seen.TOMBSTONED
    await store.due(NOW + TTL + timedelta(seconds=1), 10)
    assert await store.seen("toolu_old") is Seen.ABSENT
    assert await store.seen("toolu_new") is Seen.TOMBSTONED


async def test_a_record_that_keeps_failing_is_never_deleted(store: SqlitePendingStore) -> None:
    await store.upsert(ADDRESS, {"event": {}}, (), now=NOW)
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE)
    await store.due(NOW + timedelta(days=30), 10)
    assert await store.seen(ADDRESS) is Seen.LIVE


async def test_opening_twice_is_harmless(platform: LocalPlatform) -> None:
    first = await open_store(platform)
    await first.upsert(ADDRESS, {"event": {}}, (), now=NOW)
    second = await open_store(platform)
    assert await second.seen(ADDRESS) is Seen.LIVE


async def test_a_second_store_on_the_same_file_sees_the_first_ones_records(
    tmp_path: Path,
) -> None:
    async with create_local_platform(tmp_path) as one:
        await (await open_store(one)).upsert(ADDRESS, {"event": {}}, (), now=NOW)
    async with create_local_platform(tmp_path) as two:
        assert await (await open_store(two)).seen(ADDRESS) is Seen.LIVE


async def test_collections_do_not_see_each_other(platform: LocalPlatform) -> None:
    one = await open_store(platform)
    other = await SqlitePendingStore.open(
        db=platform.sqlite, collection="elsewhere", join_wait=JOIN_WAIT
    )
    await one.upsert(ADDRESS, {"event": {}}, (), now=NOW)
    assert await other.seen(ADDRESS) is Seen.ABSENT
