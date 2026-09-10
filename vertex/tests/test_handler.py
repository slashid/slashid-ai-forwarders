"""End-to-end tests for ``handler.run_tick`` — polling loop composition.

Mocks the ``EventSource`` + ``CheckpointStore`` boundaries and captures
``push_invocations`` calls via monkeypatch (same pattern bedrock uses).
The pipeline in between (normalize → finalize → build event → push) is
real; that's the whole point of testing at this layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiRequestBody,
    GeminiResponse,
)

from slashid_vertex_forwarder import handler
from slashid_vertex_forwarder.checkpoint_store import Checkpoint
from slashid_vertex_forwarder.config import Config
from slashid_vertex_forwarder.event_source import Entry


def _config() -> Config:
    return Config(
        endpoint="https://api.slashid.com",
        push_token="t" * 32,
        gcp_project_id="vertex-test-507702",
        gcp_region="us-central1",
    )


def _entry(*, request_id: str = "42") -> Entry:
    return Entry(
        request_id=request_id,
        logging_time=datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC),
        model_path="publishers/google/models/gemini-2.5-flash",
        region="us-central1",
        request_body=GeminiRequestBody.model_validate(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        ),
        response_body=GeminiResponse.model_validate(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "ack"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 7,
                },
            }
        ),
        api_method="GenerateContent",
    )


class _FakeSource:
    def __init__(self, entries: list[Entry]) -> None:
        self._entries = entries
        self.fetched_with: list[Checkpoint] = []

    def fetch(self, checkpoint: Checkpoint) -> list[Entry]:
        self.fetched_with.append(checkpoint)
        return self._entries


class _FakeStore:
    def __init__(self, initial: Checkpoint | None = None) -> None:
        self._current = initial or Checkpoint(None, None)
        self.saves: list[Checkpoint] = []

    def load(self) -> Checkpoint:
        return self._current

    def save(self, checkpoint: Checkpoint) -> None:
        self.saves.append(checkpoint)
        self._current = checkpoint


def _install_fake_push(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {"events": []}

    async def _fake_push(_client: Any, events: list[Any], **_kw: Any) -> int:
        captured["events"].extend(events)
        return len(events)

    monkeypatch.setattr(handler, "push_invocations", _fake_push)
    return captured


def test_run_tick_pushes_events_and_advances_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(request_id="42")
    source = _FakeSource([entry])
    store = _FakeStore()
    captured = _install_fake_push(monkeypatch)

    result = handler.run_tick(source=source, checkpoint_store=store, config=_config())

    assert result == {"events_pushed": 1, "rows_seen": 1}
    assert len(captured["events"]) == 1
    ev = captured["events"][0]
    assert ev.request_id == "42"
    assert ev.parsed_as == "vertex-gemini-generate"
    # Wire identity_details is the empty GCP shape (v1 punts on correlation).
    assert ev.identity_details.kind == "gcp"
    # Checkpoint advanced to the last entry.
    assert store.saves == [entry.checkpoint]


def test_run_tick_empty_batch_does_not_save_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No new rows → no checkpoint save, no push."""
    source = _FakeSource([])
    store = _FakeStore()
    captured = _install_fake_push(monkeypatch)

    result = handler.run_tick(source=source, checkpoint_store=store, config=_config())

    assert result == {"events_pushed": 0, "rows_seen": 0}
    assert captured["events"] == []
    assert store.saves == []


def test_run_tick_passes_current_checkpoint_to_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source must receive the loaded checkpoint (not a fresh empty one)."""
    starting = Checkpoint(
        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        id="prev",
    )
    source = _FakeSource([])
    store = _FakeStore(initial=starting)
    _install_fake_push(monkeypatch)

    handler.run_tick(source=source, checkpoint_store=store, config=_config())

    assert source.fetched_with == [starting]


def test_run_tick_batch_advances_to_last_entry_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-row batch: checkpoint jumps to the last row's watermark, not
    per-row — one save at the end of the batch."""
    entries = [_entry(request_id="1"), _entry(request_id="2"), _entry(request_id="3")]
    source = _FakeSource(entries)
    store = _FakeStore()
    captured = _install_fake_push(monkeypatch)

    handler.run_tick(source=source, checkpoint_store=store, config=_config())

    assert len(captured["events"]) == 3
    assert store.saves == [entries[-1].checkpoint]


def test_run_tick_skips_checkpoint_save_on_push_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``push_invocations`` raises, run_tick propagates (Cloud Function
    returns error) and checkpoint stays at the pre-tick value — next tick
    reprocesses (server dedupes on request_id)."""
    entry = _entry(request_id="42")
    source = _FakeSource([entry])
    store = _FakeStore()

    async def _raising_push(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(handler, "push_invocations", _raising_push)

    with pytest.raises(RuntimeError, match="upstream 500"):
        handler.run_tick(source=source, checkpoint_store=store, config=_config())

    assert store.saves == []


def test_run_tick_populates_wire_tokens_from_usage_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _FakeSource([_entry()])
    store = _FakeStore()
    captured = _install_fake_push(monkeypatch)

    handler.run_tick(source=source, checkpoint_store=store, config=_config())

    ev = captured["events"][0]
    assert ev.tokens.input == 5
    assert ev.tokens.output == 2
