"""The response reader's tail read, second pass and time budget."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from slashid_ai_forwarder_core.platform import Checkpoint

from slashid_anthropic_forwarder.compliance.client import ComplianceClient, TranscriptTooLong
from slashid_anthropic_forwarder.compliance.responses import read_responses
from slashid_anthropic_forwarder.store import Retirement
from tests.compliance_fixtures import transport
from tests.test_cursors import _FakeStore as FakeCheckpoints
from tests.test_pending import Sink, a_store, seed
from tests.test_pending import config as a_config
from tests.test_responses import ORG, _cursors, addresses

T = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
# Just after the recorded corpus: with the default 2h TTL the horizon is
# 06:05, so sessions 1, 2 and 4 have turns inside it and 5 and 6 do not.
AFTER_CORPUS = datetime(2026, 9, 21, 8, 0, 0, tzinfo=UTC)


def _message(i: int, role: str, at: datetime, model: str | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {
        "type": "message",
        "id": f"m{i}",
        "role": role,
        "created_at": at.isoformat(),
        "content": [{"type": "text", "text": f"message {i}"}],
    }
    if model:
        m["model"] = model
    return m


def _paged(messages: list[dict[str, Any]], per_page: int) -> tuple[httpx.AsyncClient, list[str]]:
    """Serves ``messages`` newest-first, ``per_page`` at a time."""
    pages: list[str] = []
    newest_first = messages[::-1]

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("page") or 0)
        pages.append(str(start))
        chunk = newest_first[start : start + per_page]
        more = start + per_page < len(newest_first)
        return httpx.Response(
            200, json={"data": chunk, "next_page": str(start + per_page) if more else None}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), pages


TRANSCRIPT = [
    _message(0, "user", T - timedelta(hours=3)),
    _message(1, "assistant", T - timedelta(hours=3), model="claude-opus-5"),
    _message(2, "user", T - timedelta(hours=1)),
    _message(3, "assistant", T - timedelta(hours=1), model="claude-opus-5"),
    _message(4, "user", T - timedelta(minutes=10)),
    _message(5, "assistant", T - timedelta(minutes=5), model="claude-opus-5"),
]


async def test_the_tail_stops_at_the_first_assistant_message_before_the_horizon() -> None:
    http, pages = _paged(TRANSCRIPT, per_page=2)
    tail, whole = await ComplianceClient(http, api_key="k").session_tail(
        "clls_x", horizon=T - timedelta(hours=2)
    )
    # m1 is kept: it bounds the round m3 consumed. m0 is not needed.
    assert [m.id for m in tail] == ["m1", "m2", "m3", "m4", "m5"]
    assert whole is False
    assert pages == ["0", "2", "4"]


async def test_a_tail_with_no_boundary_is_the_whole_transcript() -> None:
    http, _ = _paged(TRANSCRIPT, per_page=4)
    tail, whole = await ComplianceClient(http, api_key="k").session_tail(
        "clls_x", horizon=T - timedelta(days=1)
    )
    assert [m.id for m in tail] == [m["id"] for m in TRANSCRIPT]
    assert whole is True


def _full_reads(seen: list[httpx.Request]) -> list[str]:
    """Transcript reads that were not the newest-first tail."""
    return [
        r.url.path
        for r in seen
        if "/apps/sessions/local/" in r.url.path and r.url.params.get("order") != "desc"
    ]


async def _run(store, client: httpx.AsyncClient, cursors=None, **over: Any):
    return await read_responses(
        ComplianceClient(client, api_key="k"),
        store=store,
        cursors=cursors or _cursors(),
        config=a_config(compliance_key="sk-ant-api01-x", organization_uuid=ORG, **over),
        http=Sink().client(),
        now=AFTER_CORPUS,
    )


async def test_nothing_to_emit_reads_only_tails() -> None:
    """A long session costs one page when every turn in it is handled."""
    store = a_store()
    for n in (1, 2, 4):
        for address in addresses(n):
            await seed(store, address)
            await store.retire(address, Retirement.PUSHED, now=AFTER_CORPUS)
    client, seen = transport()
    counters = await _run(store, client)
    assert counters.tombstoned >= 5
    assert _full_reads(seen) == []


async def test_a_turn_the_hook_never_saw_fetches_its_full_transcript_once() -> None:
    client, seen = transport()
    counters = await _run(a_store(), client)
    assert counters.emitted >= 5
    full = _full_reads(seen)
    # Sessions 1, 2 and 4 each have turns to emit, and are read whole once.
    assert len(full) == 3 and len(set(full)) == 3


async def test_the_budget_stops_the_reader_and_holds_its_watermarks() -> None:
    """However large a conversation gets, the reader cannot take the tick
    down: Reader A and the flush run after it."""

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"data": [], "next_page": None})

    sessions, chats = FakeCheckpoints(), FakeCheckpoints()
    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    started = asyncio.get_running_loop().time()
    counters = await _run(
        a_store(),
        client,
        _cursors(sessions=sessions, chats=chats),
        response_reader_budget_seconds=0.1,
    )
    assert asyncio.get_running_loop().time() - started < 2
    assert counters.budget_exhausted
    # Only the cold start is saved; no drain finished, so nothing advances it.
    assert sessions.saves == [Checkpoint(AFTER_CORPUS, None)]
    assert chats.saves in ([], [Checkpoint(AFTER_CORPUS, None)])


async def test_a_transcript_over_the_cap_stops_being_read() -> None:
    """Memory is what the cap protects: at most one page past it is held."""
    http, pages = _paged(TRANSCRIPT, per_page=2)
    with pytest.raises(TranscriptTooLong):
        await ComplianceClient(http, api_key="k").session_messages("clls_x", max_messages=3)
    assert len(pages) == 2


async def test_a_session_too_long_to_hold_is_emitted_from_its_tail(monkeypatch) -> None:
    """The 0.1.3 reader ran out of memory holding two long sessions whole.
    Past the cap, the turn ships with the tail as its input."""

    async def too_long(self, session_id, **_):
        raise TranscriptTooLong(session_id)

    monkeypatch.setattr(ComplianceClient, "session_messages", too_long)
    client, seen = transport()
    counters = await _run(a_store(), client)
    assert counters.emitted >= 5
    assert counters.emitted_from_tail == counters.emitted - counters.from_chats
    assert _full_reads(seen) == []
