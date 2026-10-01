from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.platform import Checkpoint
from slashid_ai_forwarder_core.platform.local import create_local_platform

from slashid_codex.state import RecordStoreBusy, SqliteFileRecordStore, connect

T0 = datetime(2026, 9, 30, 12, tzinfo=UTC)


def _file(name: str, provenance: str = "tool_result") -> AIAccessedFile:
    return AIAccessedFile(
        name=name,
        content_hashes={"sha256": name.encode().hex()},
        byte_length=3,
        provenance=provenance,  # ty: ignore[invalid-argument-type]
    )


def _store(tmp_path: Path, now: datetime = T0) -> SqliteFileRecordStore:
    return SqliteFileRecordStore(lambda: connect(tmp_path), clock=lambda: now)


def test_round_lists_attachments_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_call("s1", "turn-1", "call_a", _file("/r/a.md"))
    store.put_turn(
        "s1", "turn-1", [_file("/a/x.pdf", "attachment"), _file("/a/y.png", "attachment")]
    )
    store.put_call("s1", "turn-1", "call_b", _file("/r/b.md"))
    store.put_call("s2", "turn-9", "call_a", _file("/other.md"))
    got = store.for_round("s1", ["turn-1"], ["call_a", "call_b"])
    assert [f.name for f in got] == ["/a/x.pdf", "/a/y.png", "/r/a.md", "/r/b.md"]
    assert got[0] == _file("/a/x.pdf", "attachment")


def test_round_selects_by_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_turn("s1", "turn-1", [_file("/a/x.pdf", "attachment")])
    store.put_call("s1", "turn-1", "call_a", _file("/r/a.md"))
    store.put_call("s1", "turn-1", "call_b", _file("/r/b.md"))
    assert [f.name for f in store.for_round("s1", [], ["call_b"])] == ["/r/b.md"]
    assert [f.name for f in store.for_round("s1", ["turn-1"], [])] == ["/a/x.pdf"]
    assert store.for_round("s1", [], []) == []


def test_put_replaces(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_turn("s1", "turn-1", [_file("/a/old.pdf", "attachment")])
    store.put_turn("s1", "turn-1", [_file("/a/new.pdf", "attachment")])
    store.put_call("s1", "turn-1", "call_a", _file("/r/old.md"))
    store.put_call("s1", "turn-1", "call_a", _file("/r/new.md"))
    got = store.for_round("s1", ["turn-1"], ["call_a"])
    assert [f.name for f in got] == ["/a/new.pdf", "/r/new.md"]


def test_delete_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_turn("s1", "turn-1", [_file("/a/x.pdf", "attachment")])
    store.put_call("s1", "turn-1", "call_a", _file("/r/a.md"))
    store.put_call("s1", "turn-1", "call_b", _file("/r/b.md"))
    store.delete_keys("s1", ["turn-1"], ["call_a"])
    assert [f.name for f in store.for_round("s1", ["turn-1"], ["call_a", "call_b"])] == ["/r/b.md"]


def test_delete_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_turn("s1", "turn-1", [_file("/a/x.pdf", "attachment")])
    store.put_call("s1", "turn-1", "call_a", _file("/r/a.md"))
    store.put_call("s1", "turn-2", "call_b", _file("/r/b.md"))
    store.delete_turn("s1", "turn-1")
    got = store.for_round("s1", ["turn-1", "turn-2"], ["call_a", "call_b"])
    assert [f.name for f in got] == ["/r/b.md"]


def test_delete_older_than(tmp_path: Path) -> None:
    _store(tmp_path, T0 - timedelta(days=8)).put_call("s1", "t", "call_old", _file("/old"))
    store = _store(tmp_path)
    store.put_call("s1", "t", "call_new", _file("/new"))
    store.delete_older_than(T0 - timedelta(days=7))
    assert [f.name for f in store.for_round("s1", [], ["call_old", "call_new"])] == ["/new"]


def test_two_connections_write_concurrently(tmp_path: Path) -> None:
    stores = [SqliteFileRecordStore(lambda: connect(tmp_path)) for _ in range(2)]
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def _write(i: int) -> None:
        try:
            start.wait()
            for n in range(50):
                stores[i].put_call("s1", "t", f"call_{i}_{n}", _file(f"/f{i}_{n}"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    ids = [f"call_{i}_{n}" for i in range(2) for n in range(50)]
    assert len(stores[0].for_round("s1", [], ids)) == 100


def _hammer(store: SqliteFileRecordStore, threads: int = 2, n: int = 50) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(threads)

    def _write(i: int) -> None:
        try:
            start.wait()
            for k in range(n):
                store.put_call("s1", "t", f"call_{i}_{k}", _file(f"/f{i}_{k}"))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=_write, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    return errors


def test_one_store_writes_from_two_threads(tmp_path: Path) -> None:
    opened: list[int] = []

    def _connect() -> sqlite3.Connection:
        opened.append(threading.get_ident())
        return connect(tmp_path)

    store = SqliteFileRecordStore(_connect)
    assert _hammer(store) == []
    assert len(set(opened)) == 3
    ids = [f"call_{i}_{k}" for i in range(2) for k in range(50)]
    assert len(store.for_round("s1", [], ids)) == 100


@pytest.mark.parametrize("autocommit", [True, False, sqlite3.LEGACY_TRANSACTION_CONTROL])
def test_store_on_any_transaction_mode(tmp_path: Path, autocommit: bool | int) -> None:
    db = tmp_path / "db.sqlite3"
    store = SqliteFileRecordStore(
        lambda: sqlite3.connect(db, autocommit=autocommit)  # ty: ignore[invalid-argument-type]
    )
    assert _hammer(store) == []
    store.put_call("s1", "t", "call_a", _file("/r/a.md"))
    other = sqlite3.connect(db)
    try:
        assert other.execute("SELECT COUNT(*) FROM codex_file_records").fetchone()[0] == 101
    finally:
        other.close()


def test_failed_write_rolls_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_turn("s1", "turn-1", [_file("/a/x.pdf", "attachment")])
    trigger = connect(tmp_path)
    with trigger:
        trigger.execute(
            "CREATE TRIGGER no_bad BEFORE INSERT ON codex_file_records"
            " WHEN NEW.entry_json LIKE '%/a/bad%' BEGIN SELECT RAISE(ABORT, 'bad'); END"
        )
    trigger.close()
    with pytest.raises(sqlite3.IntegrityError):
        store.put_turn("s1", "turn-1", [_file("/a/bad.pdf", "attachment")])
    assert [f.name for f in store.for_round("s1", ["turn-1"], [])] == ["/a/x.pdf"]
    store.put_turn("s1", "turn-1", [_file("/a/y.pdf", "attachment")])
    assert [f.name for f in store.for_round("s1", ["turn-1"], [])] == ["/a/y.pdf"]


def test_busy_write_gives_up_within_a_second(tmp_path: Path) -> None:
    store = SqliteFileRecordStore(lambda: connect(tmp_path))
    blocker = connect(tmp_path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(RecordStoreBusy):
            store.put_call("s1", "t", "call_a", _file("/r/a.md"))
        assert 0.5 < time.monotonic() - started < 2.5
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    store.put_call("s1", "t", "call_a", _file("/r/a.md"))
    assert [f.name for f in store.for_round("s1", [], ["call_a"])] == ["/r/a.md"]


def test_connect_uses_wal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
    finally:
        conn.close()
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "state" / "data.sqlite").stat().st_mode & 0o777 == 0o600


async def test_shares_the_local_platform_database(tmp_path: Path) -> None:
    store = SqliteFileRecordStore(lambda: connect(tmp_path))
    async with create_local_platform(tmp_path) as platform:
        checkpoints = platform.checkpoint_store(collection="c", document="s1")
        await checkpoints.save(Checkpoint(T0, "resp_1"))
        store.put_call("s1", "t", "call_a", _file("/r/a.md"))
        assert await checkpoints.load() == Checkpoint(T0, "resp_1")
    assert [f.name for f in store.for_round("s1", [], ["call_a"])] == ["/r/a.md"]
