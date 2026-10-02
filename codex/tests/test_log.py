from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from slashid_codex.log import LogReset, SessionLog
from slashid_codex.rollout import SessionMeta, parse_line

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
PARENT_ID = "01a0f553-7026-70e1-ae0c-d833daddaa9e"


def _raw(name: str) -> list[bytes]:
    return (ROLLOUTS / f"{name}.jsonl").read_bytes().splitlines(keepends=True)


def _modelled(raw: list[bytes]) -> int:
    return sum(parse_line(line) is not None for line in raw)


def _no_parent(thread_id: str) -> Path | None:
    del thread_id
    return None


def test_refresh_appends_complete_lines(tmp_path: Path) -> None:
    raw = _raw("script")
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"".join(raw[:10]))
    log = SessionLog.open(path, _no_parent)
    assert log.lines == []
    assert log.refresh() == _modelled(raw[:10])
    with path.open("ab") as f:
        f.write(b"".join(raw[10:]))
    assert log.refresh() == _modelled(raw[10:])
    assert len(log.lines) == _modelled(raw)
    assert log.refresh() == 0
    assert log.offset == path.stat().st_size
    assert log.lines[-1].offset_after == log.offset
    assert not any(line.inherited for line in log.lines)


def test_half_written_line_waits_for_its_newline(tmp_path: Path) -> None:
    raw = _raw("script")
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(raw[0] + raw[13][:40])
    log = SessionLog.open(path, _no_parent)
    assert log.refresh() == 1
    with path.open("ab") as f:
        f.write(raw[13][40:])
    assert log.refresh() == 1
    assert log.lines[-1].line.type == "token_usage_record"
    assert log.lines[-1].offset_after == len(raw[0]) + len(raw[13])


def test_unparseable_lines_are_skipped_and_counted(tmp_path: Path) -> None:
    raw = _raw("script")
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(
        raw[0] + b'{"type": "token_usage_record", "payload": {}}\n{broken\n\n' + raw[13]
    )
    log = SessionLog.open(path, _no_parent)
    assert log.refresh() == 2
    assert log.skipped == 2


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/fd")
def test_no_handle_left_open(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"".join(_raw("script")))
    log = SessionLog.open(path, _no_parent)
    before = len(os.listdir("/proc/self/fd"))
    log.refresh()
    assert len(os.listdir("/proc/self/fd")) == before


def test_truncated_file_resets(tmp_path: Path) -> None:
    raw = _raw("script")
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"".join(raw))
    log = SessionLog.open(path, _no_parent)
    log.refresh()
    with path.open("r+b") as f:
        f.truncate(len(raw[0]))
    with pytest.raises(LogReset):
        log.refresh()


def test_replaced_file_resets(tmp_path: Path) -> None:
    raw = _raw("script")
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"".join(raw))
    log = SessionLog.open(path, _no_parent)
    log.refresh()
    other = tmp_path / "other.jsonl"
    other.write_bytes(b"".join(raw) + raw[-1])
    os.replace(other, path)
    with pytest.raises(LogReset):
        log.refresh()


def test_fork_starts_with_parent_lines(tmp_path: Path) -> None:
    parent = tmp_path / "parent.jsonl"
    shutil.copy(ROLLOUTS / "compaction.jsonl", parent)
    fork = tmp_path / "fork.jsonl"
    shutil.copy(ROLLOUTS / "fork.jsonl", fork)
    asked: list[str] = []

    def locate(thread_id: str) -> Path | None:
        asked.append(thread_id)
        return parent if thread_id == PARENT_ID else None

    log = SessionLog.open(fork, locate)
    log.refresh()
    assert asked == [PARENT_ID]
    assert not log.history_truncated
    inherited = [line for line in log.lines if line.inherited]
    assert len(inherited) == _modelled(_raw("compaction")[:45])
    assert log.lines[: len(inherited)] == inherited
    first_own = log.lines[len(inherited)].line.payload
    assert isinstance(first_own, SessionMeta)
    assert first_own.history_base is not None
    assert inherited[-1].offset_after == first_own.history_base.end_byte_offset
    assert len(log.lines) - len(inherited) == _modelled(_raw("fork"))


def test_fork_with_missing_parent_is_truncated(tmp_path: Path) -> None:
    fork = tmp_path / "fork.jsonl"
    shutil.copy(ROLLOUTS / "fork.jsonl", fork)
    log = SessionLog.open(fork, _no_parent)
    log.refresh()
    assert log.history_truncated
    assert not any(line.inherited for line in log.lines)
    assert len(log.lines) == _modelled(_raw("fork"))
