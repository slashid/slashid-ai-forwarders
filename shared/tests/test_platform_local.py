"""``LocalPlatform``: what is specific to SQLite. What it shares with the
Firestore platform is in ``test_platform_contract.py``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from slashid_ai_forwarder_core import platform as platforms
from slashid_ai_forwarder_core.platform import Checkpoint
from slashid_ai_forwarder_core.platform.local import LocalPlatform, create_local_platform

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
TICK = timedelta(minutes=10)


@pytest.fixture
async def platform() -> AsyncIterator[LocalPlatform]:
    async with create_local_platform(":memory:") as built:
        yield built


async def test_the_sqlite_property_is_the_open_connection(platform: LocalPlatform) -> None:
    cursor = await platform.sqlite.execute("SELECT 1")
    assert await cursor.fetchone() == (1,)


async def test_get_resolves_the_local_platform() -> None:
    async with platforms.get("local", path=":memory:") as built:
        assert isinstance(built, LocalPlatform)


def test_a_missing_path_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        platforms.get("local")


# --- checkpoints -------------------------------------------------------------


async def test_a_timestamp_round_trips_to_the_microsecond(platform: LocalPlatform) -> None:
    store = platform.checkpoint_store(collection="c", document="d")
    when = datetime(2026, 9, 5, 2, 43, 59, 123456, tzinfo=UTC)
    await store.save(Checkpoint(timestamp=when, id="42"))
    assert await store.load() == Checkpoint(timestamp=when, id="42")


async def test_a_non_utc_timestamp_loads_as_the_same_instant_in_utc(
    platform: LocalPlatform,
) -> None:
    store = platform.checkpoint_store(collection="c", document="d")
    when = datetime(2026, 9, 5, 4, 43, 59, 654321, tzinfo=timezone(timedelta(hours=2)))
    await store.save(Checkpoint(timestamp=when, id="42"))
    loaded = await store.load()
    assert loaded.timestamp == when
    assert loaded.timestamp is not None and loaded.timestamp.utcoffset() == timedelta(0)


# --- the lease under contention ----------------------------------------------


async def test_concurrent_takers_on_one_lease_admit_exactly_one(platform: LocalPlatform) -> None:
    lease = platform.tick_lease(collection="c", document="tick")
    results = await asyncio.gather(
        *[lease.take(TICK, owner=f"tick-{n}", now=NOW) for n in range(5)]
    )
    assert sum(results) == 1


async def test_concurrent_holds_on_one_platform_admit_exactly_one(platform: LocalPlatform) -> None:
    lease = platform.tick_lease(collection="c", document="tick")
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[bool] = []

    async def hold() -> None:
        async with lease.hold(TICK) as held:
            seen.append(held)
            started.set()
            await release.wait()

    first = asyncio.create_task(hold())
    await started.wait()
    async with lease.hold(TICK) as held:
        assert held is False
    release.set()
    await first
    assert seen == [True]


# --- blobs -------------------------------------------------------------------


async def test_a_blob_is_stored_replaced_and_read_back(platform: LocalPlatform) -> None:
    sink = platform.blob_sink("bucket")
    await sink.put("a.json", b"one", content_type="application/json")
    await sink.put("a.json", b"two", content_type="application/json")
    assert await sink.get("a.json") == b"two"
    assert await sink.get("missing") is None


async def test_buckets_are_separate(platform: LocalPlatform) -> None:
    await platform.blob_sink("one").put("n", b"1", content_type="text/plain")
    assert await platform.blob_sink("two").get("n") is None


async def test_binary_data_round_trips_as_bytes(platform: LocalPlatform) -> None:
    sink = platform.blob_sink("b")
    data = bytes(range(256))
    await sink.put("n", data, content_type="application/octet-stream")
    assert await sink.get("n") == data


async def test_a_file_database_keeps_blobs_as_files_beside_it(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite"
    async with create_local_platform(path) as built:
        sink = built.blob_sink("capture")
        await sink.put("2026/frame.json", b"{}", content_type="application/json")
        file = tmp_path / "data.sqlite.blobs" / "capture" / "2026" / "frame.json"
        assert built.blobs == tmp_path / "data.sqlite.blobs"
        assert file.read_bytes() == b"{}"
        # written through a temporary file that is renamed into place
        assert [p.name for p in file.parent.iterdir()] == ["frame.json"]
    assert file.exists()  # outlives the platform, like the database


async def test_a_memory_database_keeps_blobs_in_a_temporary_directory() -> None:
    async with create_local_platform(":memory:") as built:
        await built.blob_sink("b").put("n", b"x", content_type="text/plain")
        kept = built.blobs
        assert (kept / "b" / "n").read_bytes() == b"x"
    assert not kept.exists()


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "a/../../escape"])
async def test_a_blob_name_cannot_leave_its_bucket(platform: LocalPlatform, name: str) -> None:
    sink = platform.blob_sink("b")
    with pytest.raises(ValueError):
        await sink.put(name, b"x", content_type="text/plain")
    with pytest.raises(ValueError):
        await sink.get(name)


@pytest.mark.parametrize("bucket", ["", ".", "..", "a/b", "/abs"])
def test_a_bucket_must_be_one_path_segment(platform: LocalPlatform, bucket: str) -> None:
    with pytest.raises(ValueError):
        platform.blob_sink(bucket)


# --- scheduler auth ----------------------------------------------------------


async def test_the_principal_is_the_expected_bearer_token(platform: LocalPlatform) -> None:
    check = platform.scheduler_auth(principal="secret-token", audience="ignored")
    assert await check("secret-token") is True
    assert await check("wrong") is False


async def test_a_non_ascii_token_is_refused_not_an_error(platform: LocalPlatform) -> None:
    check = platform.scheduler_auth(principal="secret-token", audience=None)
    assert await check("tökén") is False
    assert await platform.scheduler_auth(principal="tökén", audience=None)("tökén") is True


async def test_an_unset_principal_refuses_every_token(platform: LocalPlatform) -> None:
    check = platform.scheduler_auth(principal=None, audience=None)
    assert await check("anything") is False
    assert await platform.scheduler_auth(principal="", audience=None)("") is False


# --- location and lifecycle --------------------------------------------------


async def test_a_file_database_creates_its_parent_and_outlives_the_platform(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing" / "data.sqlite"
    when = datetime(2026, 9, 5, tzinfo=UTC)
    async with create_local_platform(path) as first:
        await first.checkpoint_store(collection="c", document="d").save(Checkpoint(when, "7"))
    assert path.exists()
    async with create_local_platform(path) as second:
        assert await second.checkpoint_store(collection="c", document="d").load() == Checkpoint(
            when, "7"
        )


async def test_memory_platforms_are_independent() -> None:
    async with create_local_platform(":memory:") as one, create_local_platform(":memory:") as two:
        await one.checkpoint_store(collection="c", document="d").save(Checkpoint(None, "1"))
        assert await two.checkpoint_store(collection="c", document="d").load() == Checkpoint(
            None, None
        )


async def test_the_connection_is_closed_when_the_block_ends() -> None:
    async with create_local_platform(":memory:") as built:
        pass
    with pytest.raises(ValueError):
        await built.sqlite.execute("SELECT 1")


async def test_the_connection_is_closed_when_the_block_raises() -> None:
    kept: LocalPlatform | None = None
    with pytest.raises(RuntimeError, match="boom"):
        async with create_local_platform(":memory:") as built:
            kept = built
            raise RuntimeError("boom")
    assert kept is not None
    with pytest.raises(ValueError):
        await kept.sqlite.execute("SELECT 1")


# --- two connections on one file --------------------------------------------


async def test_two_platforms_on_one_file_contend_on_the_lease(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite"
    async with create_local_platform(path) as a, create_local_platform(path) as b:
        lease_a = a.tick_lease(collection="c", document="tick")
        lease_b = b.tick_lease(collection="c", document="tick")
        assert await lease_a.take(TICK, owner="a", now=NOW) is True
        assert await lease_b.take(TICK, owner="b", now=NOW) is False


async def test_a_write_that_waits_past_the_busy_timeout_raises(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite"
    async with create_local_platform(path) as a, create_local_platform(path) as b:
        await a.sqlite.execute("PRAGMA busy_timeout=50")
        await b.sqlite.execute("BEGIN IMMEDIATE")  # holds the write lock
        try:
            lease = a.tick_lease(collection="c", document="tick")
            with pytest.raises(aiosqlite.OperationalError):
                await lease.take(TICK, owner="a", now=NOW)
        finally:
            await b.sqlite.execute("ROLLBACK")
