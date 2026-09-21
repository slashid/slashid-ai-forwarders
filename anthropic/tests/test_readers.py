"""The tick's reader pass: order, isolation, and the flush that follows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from slashid_anthropic_forwarder.compliance import readers
from slashid_anthropic_forwarder.compliance.checkpoint import FEEDS, Cursors
from tests.compliance_fixtures import transport
from tests.test_cursors import _FakeStore as FakeCheckpoints
from tests.test_pending import Sink, a_store
from tests.test_pending import config as a_config

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
ORG = "11111111-1111-1111-1111-111111111111"


def cursors() -> Cursors:
    return Cursors({feed: FakeCheckpoints() for feed in FEEDS}, poll_lag_seconds=120)


def enabled(**over: Any) -> Any:
    return a_config(**{"compliance_key": "sk-ant-api01-x", "organization_uuid": ORG, **over})


async def test_without_a_key_the_readers_do_not_run() -> None:
    client, seen = transport()
    counters = await readers.run_readers(
        store=a_store(), config=a_config(), http=client, cursors=cursors(), now=NOW
    )
    assert counters == {}
    assert seen == []


async def test_reader_b_runs_before_reader_a_and_feeds_it_the_models() -> None:
    client, seen = transport()
    counters = await readers.run_readers(
        store=a_store(),
        config=enabled(),
        http=Sink().client(),
        cursors=cursors(),
        now=NOW,
        compliance=client,
    )
    paths = [r.url.path for r in seen]
    assert paths.index("/v1/compliance/apps/sessions/local") < paths.index(
        "/v1/compliance/activities"
    )
    assert counters["denials_handled"] == 1
    assert counters["responses_emitted"] >= 1


async def test_a_failing_reader_b_does_not_stop_reader_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("429")

    monkeypatch.setattr(readers, "read_responses", boom)
    client, _ = transport()
    counters = await readers.run_readers(
        store=a_store(),
        config=enabled(),
        http=Sink().client(),
        cursors=cursors(),
        now=NOW,
        compliance=client,
    )
    # Reader A still ran, with an empty model map — the documented "unknown".
    assert counters["denials_handled"] == 1
    assert "responses_emitted" not in counters


async def test_a_failing_reader_a_does_not_fail_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("429")

    monkeypatch.setattr(readers, "read_denials", boom)
    client, _ = transport()
    counters = await readers.run_readers(
        store=a_store(),
        config=enabled(),
        http=Sink().client(),
        cursors=cursors(),
        now=NOW,
        compliance=client,
    )
    assert counters["responses_emitted"] >= 1
