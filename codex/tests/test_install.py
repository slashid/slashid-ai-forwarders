from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from slashid_codex.install import created_at


def test_created_at_stable(tmp_path: Path) -> None:
    first = created_at(tmp_path)
    assert first.tzinfo is not None
    assert created_at(tmp_path) == first
    assert (tmp_path / "created_at").exists()


@pytest.mark.parametrize("existing", ["", "garbled"])
def test_created_at_rewrites_unreadable_file(tmp_path: Path, existing: str) -> None:
    (tmp_path / "created_at").write_text(existing)
    first = created_at(tmp_path)
    assert created_at(tmp_path) == first
    assert datetime.fromisoformat((tmp_path / "created_at").read_text()) == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["created_at"]


def test_state_dir_private_despite_umask(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    old = os.umask(0o277)
    try:
        created_at(state_dir)
    finally:
        os.umask(old)
    assert state_dir.stat().st_mode & 0o777 == 0o700
