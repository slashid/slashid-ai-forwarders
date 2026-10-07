"""A signed frame becomes a pushed event on the local platform, with no scheduler."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from slashid_anthropic_forwarder import main
from slashid_anthropic_forwarder.main import create_app
from slashid_anthropic_forwarder.platform import open_backends
from tests.conftest import Signer, _no_readers, _until
from tests.test_main import TOOL_FRAME, _config
from tests.test_pending import Sink


def _rows(data_dir: Path) -> list[tuple[str, int | None]]:
    db = sqlite3.connect(f"file:{data_dir / 'data.sqlite'}?mode=ro", uri=True)
    try:
        return db.execute("select address, tombstoned_us from pending").fetchall()
    finally:
        db.close()


def _live(data_dir: Path) -> list[str]:
    return [address for address, tombstoned in _rows(data_dir) if tombstoned is None]


async def test_a_frame_is_pushed_by_the_timer_and_its_record_retired(
    sign: Signer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_ticks: None
) -> None:
    monkeypatch.setattr(main, "run_readers", _no_readers)
    config = _config(
        platform="local",
        project_id=None,
        data_dir=str(tmp_path),
        join_wait_seconds=0,
        preflight_enabled=False,
    )
    sink = Sink()
    app = create_app(config, backends=lambda: open_backends(config), client=sink.client())
    body = json.dumps(TOOL_FRAME).encode()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            verdict = await c.post("/", content=body, headers=sign(body, "msg_1"))
        assert verdict.json() == {"action": "allow"}
        await _until(lambda: not _live(tmp_path) and bool(sink.request_ids))
        pushed = list(sink.request_ids)
        for _ in range(10):
            await asyncio.sleep(0.01)
    assert sink.request_ids == pushed
    assert len(set(pushed)) == len(pushed)
    # The run the frame answers, filed under its tool_use id, and this frame's
    # own tail, whose wire id is the delivery id.
    assert sorted(pushed) == ["msg_1", "toolu_01Dqhr2d1w2UCUqbXhCSGutC"]
    rows = dict(_rows(tmp_path))
    tails = [address for address in rows if address.startswith("tail:")]
    # Two tails: this frame's, and the predecessor's, retired unwritten.
    assert sorted(rows) == sorted(["toolu_01Dqhr2d1w2UCUqbXhCSGutC", *tails])
    assert len(tails) == 2
    assert all(tombstoned is not None for tombstoned in rows.values())
