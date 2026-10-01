from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from slashid_ai_forwarder_core.platform import Checkpoint

from slashid_codex.dev_platform import DevPlatform

T0 = datetime(2026, 9, 30, 15, 33, 37, 123000, tzinfo=UTC)


def test_empty_checkpoint(tmp_path: Path) -> None:
    store = DevPlatform(tmp_path).checkpoint_store(collection="codex-rollouts", document="s1")
    assert store.load() == Checkpoint(None, None)


def test_checkpoint_persists_across_instances(tmp_path: Path) -> None:
    DevPlatform(tmp_path).checkpoint_store(collection="c", document="s1").save(
        Checkpoint(T0, "resp_1")
    )
    store = DevPlatform(tmp_path).checkpoint_store(collection="c", document="s1")
    assert store.load() == Checkpoint(T0, "resp_1")
    other = DevPlatform(tmp_path).checkpoint_store(collection="c", document="s2")
    assert other.load() == Checkpoint(None, None)


def test_checkpoint_moves_forward_only(tmp_path: Path) -> None:
    store = DevPlatform(tmp_path).checkpoint_store(collection="c", document="s1")
    store.save(Checkpoint(T0, "resp_1"))
    store.save(Checkpoint(T0 - timedelta(seconds=1), "resp_0"))
    assert store.load() == Checkpoint(T0, "resp_1")
    store.save(Checkpoint(T0, "resp_2"))
    assert store.load() == Checkpoint(T0, "resp_2")
    later = T0 + timedelta(minutes=1)
    store.save(Checkpoint(later, "resp_3"))
    assert store.load() == Checkpoint(later, "resp_3")
    store.save(Checkpoint(None, None))
    assert store.load() == Checkpoint(later, "resp_3")


def test_checkpoint_compares_across_offsets(tmp_path: Path) -> None:
    store = DevPlatform(tmp_path).checkpoint_store(collection="c", document="s1")
    store.save(Checkpoint(T0, "resp_1"))
    earlier_elsewhere = (T0 - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=5)))
    store.save(Checkpoint(earlier_elsewhere, "resp_0"))
    assert store.load() == Checkpoint(T0, "resp_1")


def test_created_at_stable(tmp_path: Path) -> None:
    first = DevPlatform(tmp_path).created_at()
    assert first.tzinfo is not None
    assert DevPlatform(tmp_path).created_at() == first
    assert (tmp_path / "created_at").exists()


def test_connect_uses_wal(tmp_path: Path) -> None:
    conn = DevPlatform(tmp_path).connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    finally:
        conn.close()
    assert (tmp_path / "state.sqlite3").exists()


def test_state_dir_created_private(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    DevPlatform(state_dir).connect().close()
    assert state_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("existing", ["", "garbled"])
def test_created_at_rewrites_unreadable_file(tmp_path: Path, existing: str) -> None:
    (tmp_path / "created_at").write_text(existing)
    first = DevPlatform(tmp_path).created_at()
    assert DevPlatform(tmp_path).created_at() == first
    assert datetime.fromisoformat((tmp_path / "created_at").read_text()) == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["created_at"]


def test_state_dir_private_despite_umask(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    old = os.umask(0o277)
    try:
        DevPlatform(state_dir).created_at()
    finally:
        os.umask(old)
    assert state_dir.stat().st_mode & 0o777 == 0o700
