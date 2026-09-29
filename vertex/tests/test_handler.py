"""End-to-end tests for ``handler.run_tick`` — polling loop composition.

Mocks the ``EventSource`` boundary and captures ``push_invocations``
calls via monkeypatch (same pattern bedrock uses). Sources own their
own checkpoint stores AND their full normalize/finalize/build_event
pipeline now; the fake source below carries a ``commits`` list to
observe checkpoint saves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIInvocationTokens,
    AIModel,
    GCPIdentityDetails,
)
from slashid_ai_forwarder_core.platform import Checkpoint

from slashid_vertex_forwarder import handler
from slashid_vertex_forwarder.config import Config


def _config() -> Config:
    return Config(
        endpoint="https://api.slashid.com",
        push_token="t" * 32,
        gcp_project_id="vertex-test-507702",
        gcp_regions=["us-central1"],
    )


def _event(
    *,
    request_id: str = "42",
    timestamp: datetime | None = None,
    model_id: str = "publishers/google/models/gemini-2.5-flash",
    parsed_as: str = "vertex-google",
    input_tokens: int = 5,
    output_tokens: int = 2,
) -> AIInvocationObservedV1:
    """Build a minimal ``AIInvocationObservedV1`` the way a source would
    hand it to the handler after running its own pipeline."""
    ts = timestamp or datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC)
    return AIInvocationObservedV1(
        request_id=request_id,
        timestamp=ts.isoformat(),
        identity_details=GCPIdentityDetails(),
        model=AIModel(
            id=model_id,
            name="gemini-2.5-flash",
            provider="google",
            raw_model_id=model_id,
        ),
        tokens=AIInvocationTokens(input=input_tokens, output=output_tokens),
        parsed_as=parsed_as,
    )


class _FakeSource:
    """Event-returning source double.

    Captures ``commit`` calls into ``commits`` so tests can assert on
    checkpoint advancement without a separate ``CheckpointStore`` mock —
    sources own their store in the new topology.
    """

    def __init__(
        self,
        events: list[AIInvocationObservedV1],
        next_checkpoint: Checkpoint | None,
        *,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self._events = events
        self._next_checkpoint = next_checkpoint
        self._raise = raise_on_fetch
        self.commits: list[Checkpoint] = []
        self.fetch_count = 0

    def fetch(self) -> tuple[list[AIInvocationObservedV1], Checkpoint | None]:
        self.fetch_count += 1
        if self._raise is not None:
            raise self._raise
        return list(self._events), self._next_checkpoint

    def commit(self, checkpoint: Checkpoint) -> None:
        self.commits.append(checkpoint)


def _install_fake_push(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {"events": []}

    async def _fake_push(_client: Any, events: list[Any], **_kw: Any) -> int:
        captured["events"].extend(events)
        return len(events)

    monkeypatch.setattr(handler, "push_invocations", _fake_push)
    return captured


def test_run_tick_pushes_events_and_commits_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev = _event(request_id="42")
    cp = Checkpoint(datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC), "42")
    source = _FakeSource([ev], cp)
    captured = _install_fake_push(monkeypatch)

    result = handler.run_tick(sources=[source], config=_config())

    assert result == {"events_pushed": 1, "envelopes_seen": 1}
    assert len(captured["events"]) == 1
    pushed = captured["events"][0]
    assert pushed.request_id == "42"
    assert pushed.parsed_as == "vertex-google"
    assert pushed.identity_details.kind == "gcp"
    # The event is passed through unchanged — no build_event_from_normalized
    # call inside the handler anymore.
    assert pushed is ev
    assert source.commits == [cp]


def test_run_tick_no_events_no_checkpoint_skips_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No raw records seen → next_checkpoint is None → no commit."""
    source = _FakeSource([], None)
    captured = _install_fake_push(monkeypatch)

    result = handler.run_tick(sources=[source], config=_config())

    assert result == {"events_pushed": 0, "envelopes_seen": 0}
    assert captured["events"] == []
    assert source.commits == []


def test_run_tick_commits_when_events_empty_but_checkpoint_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parse-failure case: fetch returns ([], Checkpoint(t, id)). commit
    should still be called so the pipeline doesn't loop on broken rows."""
    cp = Checkpoint(datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC), "raw-only-id")
    source = _FakeSource([], cp)
    _install_fake_push(monkeypatch)

    handler.run_tick(sources=[source], config=_config())

    assert source.commits == [cp]


def test_run_tick_batch_commits_last_checkpoint_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source hands over a batch of events + one final next_checkpoint —
    handler commits exactly once at that value."""
    evs = [_event(request_id=str(i)) for i in (1, 2, 3)]
    cp = Checkpoint(datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC), "3")
    source = _FakeSource(evs, cp)
    captured = _install_fake_push(monkeypatch)

    handler.run_tick(sources=[source], config=_config())

    assert len(captured["events"]) == 3
    assert source.commits == [cp]


def test_run_tick_skips_commit_on_push_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When push raises, the per-source try/except catches it and
    ``commit`` is not called — the source's checkpoint stays put and
    next tick reprocesses (server dedupes on request_id)."""
    ev = _event(request_id="42")
    cp = Checkpoint(datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC), "42")
    source = _FakeSource([ev], cp)

    async def _raising_push(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(handler, "push_invocations", _raising_push)

    # Handler catches per-source — no exception propagates.
    handler.run_tick(sources=[source], config=_config())

    assert source.commits == []


def test_run_tick_populates_wire_tokens_from_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev = _event(input_tokens=5, output_tokens=2)
    cp = Checkpoint(datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC), "42")
    source = _FakeSource([ev], cp)
    captured = _install_fake_push(monkeypatch)

    handler.run_tick(sources=[source], config=_config())

    pushed = captured["events"][0]
    assert pushed.tokens.input == 5
    assert pushed.tokens.output == 2


def test_run_tick_dispatches_multiple_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sources, each with its own events + checkpoint. Handler
    fetches both, pushes each batch, and commits each source's next
    checkpoint exactly once."""
    ev_a = _event(request_id="a1")
    ev_b1 = _event(request_id="b1")
    ev_b2 = _event(request_id="b2")
    cp_a = Checkpoint(datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC), "a1")
    cp_b = Checkpoint(datetime(2026, 9, 5, 12, 0, 5, tzinfo=UTC), "b2")
    src_a = _FakeSource([ev_a], cp_a)
    src_b = _FakeSource([ev_b1, ev_b2], cp_b)
    captured = _install_fake_push(monkeypatch)

    result = handler.run_tick(sources=[src_a, src_b], config=_config())

    assert result == {"events_pushed": 3, "envelopes_seen": 3}
    assert len(captured["events"]) == 3
    assert src_a.commits == [cp_a]
    assert src_b.commits == [cp_b]
    assert src_a.fetch_count == 1
    assert src_b.fetch_count == 1


def test_run_tick_isolates_source_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Source A raises inside fetch; source B's fetch/push/commit still
    runs. Error is logged; run_tick returns success-side counters only."""
    ev_b = _event(request_id="b1")
    cp_b = Checkpoint(datetime(2026, 9, 5, 12, 0, 5, tzinfo=UTC), "b1")
    src_a = _FakeSource([], None, raise_on_fetch=RuntimeError("boom"))
    src_b = _FakeSource([ev_b], cp_b)
    _install_fake_push(monkeypatch)

    with caplog.at_level("ERROR"):
        result = handler.run_tick(sources=[src_a, src_b], config=_config())

    assert result == {"events_pushed": 1, "envelopes_seen": 1}
    assert src_a.commits == []
    assert src_b.commits == [cp_b]
    assert any("_FakeSource failed this tick" in r.message for r in caplog.records)
