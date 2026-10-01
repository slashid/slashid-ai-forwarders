from __future__ import annotations

import os
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from slashid_ai_forwarder_core.platform.checkpoint import Checkpoint

from slashid_codex.cache import SessionCache, locate
from slashid_codex.rollout import TokenUsageRecord, parse_line

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
SCRIPT_ID = "01a0f397-f16e-7d83-87e7-6701f1b384c7"
FORK_ID = "01a0f557-8847-7820-9b53-5f12b00470d2"
PARENT_ID = "01a0f553-7026-70e1-ae0c-d833daddaa9e"
EMPTY = Checkpoint(timestamp=None, id=None)
T0 = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _response_ids(name: str) -> list[str]:
    out = []
    for raw in (ROLLOUTS / f"{name}.jsonl").read_bytes().splitlines():
        line = parse_line(raw)
        if line is not None and isinstance(line.payload, TokenUsageRecord):
            out.append(line.payload.response_id)
    return out


def _install(codex_home: Path, name: str, session_id: str) -> Path:
    day = codex_home / "sessions" / "2026" / "09" / "30"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-30T15-33-36-{session_id}.jsonl"
    shutil.copy(ROLLOUTS / f"{name}.jsonl", path)
    return path


def _cache(tmp_path: Path, watermarks: dict[str, Checkpoint] | None = None):
    clock = _Clock()
    marks = watermarks or {}
    cache = SessionCache(
        codex_home=tmp_path / ".codex",
        load_watermark=lambda session_id: marks.get(session_id, EMPTY),
        clock=clock,
    )
    return cache, clock


def test_get_builds_and_reuses_a_session(tmp_path: Path) -> None:
    path = _install(tmp_path / ".codex", "script", SCRIPT_ID)
    first_id = _response_ids("script")[0]
    cache, _ = _cache(tmp_path, {SCRIPT_ID: Checkpoint(timestamp=None, id=first_id)})
    session = cache.get(SCRIPT_ID, path)
    assert isinstance(session.lock, type(threading.Lock()))
    assert session.head.at_end
    assert not session.session_started
    assert session.last_hook_at is None
    assert not session.batch_in_flight
    sent = session.send.next_closed()
    assert sent is not None
    assert sent.response_id == _response_ids("script")[1]
    assert cache.get(SCRIPT_ID, path) is session


def test_log_reset_recreates_log_and_cursors(tmp_path: Path) -> None:
    path = _install(tmp_path / ".codex", "script", SCRIPT_ID)
    cache, _ = _cache(tmp_path)
    session = cache.get(SCRIPT_ID, path)
    assert session.send.next_closed() is not None
    old = (session.log, session.head, session.send)
    replacement = path.with_suffix(".tmp")
    shutil.copy(path, replacement)
    os.replace(replacement, path)
    session.refresh()
    assert session.log is not old[0]
    assert session.head is not old[1]
    assert session.send is not old[2]
    assert session.head.at_end
    # The new send cursor starts again from the watermark (empty here).
    assert [r.response_id for r in iter(session.send.next_closed, None)] == _response_ids("script")


def test_refresh_advances_the_head(tmp_path: Path) -> None:
    raw = (ROLLOUTS / "script.jsonl").read_bytes().splitlines(keepends=True)
    path = _install(tmp_path / ".codex", "script", SCRIPT_ID)
    path.write_bytes(b"".join(raw[:10]))
    cache, _ = _cache(tmp_path)
    session = cache.get(SCRIPT_ID, path)
    with path.open("ab") as f:
        f.write(b"".join(raw[10:]))
    assert session.refresh() > 0
    assert session.head.at_end
    assert session.head.pending_call("call_Y6yRs2JecdOylBbDtr6XaqwV") is not None


def test_fork_parent_located_under_codex_home(tmp_path: Path) -> None:
    _install(tmp_path / ".codex", "compaction", PARENT_ID)
    path = _install(tmp_path / ".codex", "fork", FORK_ID)
    cache, _ = _cache(tmp_path)
    session = cache.get(FORK_ID, path)
    assert not session.log.history_truncated
    assert any(line.inherited for line in session.log.lines)
    assert [r.response_id for r in iter(session.send.next_closed, None)] == _response_ids("fork")


def test_eviction_predicate(tmp_path: Path) -> None:
    path = _install(tmp_path / ".codex", "script", SCRIPT_ID)
    cache, clock = _cache(tmp_path)
    session = cache.get(SCRIPT_ID, path)
    # Loaded by the sweep: not started, but the send cursor has a backlog.
    assert not cache.evictable(session)
    while session.send.next_closed() is not None:
        pass
    assert cache.evictable(session)

    session.batch_in_flight = True
    assert not cache.evictable(session)
    session.batch_in_flight = False

    cache.touch_hook(SCRIPT_ID)
    assert session.session_started
    assert session.last_hook_at == T0
    assert not cache.evictable(session)
    clock.now = T0 + timedelta(minutes=10)
    assert not cache.evictable(session)
    clock.now = T0 + timedelta(minutes=10, seconds=1)
    assert cache.evictable(session)

    clock.now = T0
    cache.touch_hook(SCRIPT_ID)
    cache.end(SCRIPT_ID)
    assert not session.session_started
    assert cache.evictable(session)


def test_evict_drops_sessions_and_get_rebuilds(tmp_path: Path) -> None:
    path = _install(tmp_path / ".codex", "script", SCRIPT_ID)
    cache, _ = _cache(tmp_path)
    session = cache.get(SCRIPT_ID, path)
    assert cache.evict() == []
    while session.send.next_closed() is not None:
        pass
    assert cache.evict() == [SCRIPT_ID]
    rebuilt = cache.get(SCRIPT_ID, path)
    assert rebuilt is not session
    assert len(rebuilt.log.lines) == len(session.log.lines)


def test_evict_skips_a_locked_session(tmp_path: Path) -> None:
    path = _install(tmp_path / ".codex", "script", SCRIPT_ID)
    cache, _ = _cache(tmp_path)
    session = cache.get(SCRIPT_ID, path)
    while session.send.next_closed() is not None:
        pass
    with session.lock:
        assert cache.evict() == []
    assert cache.evict() == [SCRIPT_ID]


def test_touch_and_end_ignore_unknown_sessions(tmp_path: Path) -> None:
    cache, _ = _cache(tmp_path)
    cache.touch_hook("nope")
    cache.end("nope")


SUB = "01a0f38b-f3a4-7c70-95e2-420a7fcbcc03"
EXACT_NAME = f"rollout-2026-09-30T15-20-30-{SUB}.jsonl"
SUFFIXED_NAME = f"rollout-2026-09-30T15-27-32-{SUB}_01a0f392-636d-76a3-bf28-01724377e2b4.jsonl"


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    os.utime(path, (mtime, mtime))
    return path


def test_locate_prefers_transcript_path(tmp_path: Path) -> None:
    transcript = _touch(tmp_path / "elsewhere.jsonl", 1)
    _touch(tmp_path / ".codex" / "sessions" / EXACT_NAME, 2)
    assert locate(SUB, transcript, tmp_path / ".codex") == transcript


def test_locate_searches_sessions_and_archive(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    assert locate(SUB, None, home) is None
    assert locate(SUB, tmp_path / "gone.jsonl", home) is None
    nested = _touch(home / "sessions" / "2026" / "09" / "30" / EXACT_NAME, 1)
    assert locate(SUB, tmp_path / "gone.jsonl", home) == nested


def test_locate_picks_the_most_recent_of_several(tmp_path: Path) -> None:
    # Measured: both files carry the same session id; the exact name is an
    # abandoned first rollout and the suffixed one holds the session.
    home = tmp_path / ".codex"
    exact = _touch(home / "archived_sessions" / EXACT_NAME, 1_000)
    suffixed = _touch(home / "archived_sessions" / SUFFIXED_NAME, 2_000)
    assert locate(SUB, None, home) == suffixed
    os.utime(exact, (3_000, 3_000))
    assert locate(SUB, None, home) == exact


@pytest.mark.parametrize("name", [f"rollout-x-{SUB}x.jsonl", f"rollout-x-{SUB}.json"])
def test_locate_ignores_other_names(tmp_path: Path, name: str) -> None:
    _touch(tmp_path / ".codex" / "sessions" / name, 1)
    assert locate(SUB, None, tmp_path / ".codex") is None
