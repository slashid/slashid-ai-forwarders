"""Three watermarks, and the three ways a reader loses data without them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from slashid_ai_forwarder_core.checkpoint import Checkpoint

from slashid_anthropic_forwarder.compliance.checkpoint import (
    ACTIVITIES,
    CHATS,
    SESSIONS,
    Cursors,
)

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
LAG = 120


class _FakeStore:
    def __init__(self) -> None:
        self.value = Checkpoint(None, None)
        self.saves: list[Checkpoint] = []

    def load(self) -> Checkpoint:
        return self.value

    def save(self, checkpoint: Checkpoint) -> None:
        self.value = checkpoint
        self.saves.append(checkpoint)


def _cursors() -> tuple[Cursors, dict[str, _FakeStore]]:
    stores = {feed: _FakeStore() for feed in (ACTIVITIES, CHATS, SESSIONS)}
    return Cursors(stores, poll_lag_seconds=LAG), stores


def test_a_cold_start_is_the_lag_window_not_a_backfill() -> None:
    cursors, _ = _cursors()
    assert cursors.window_start(SESSIONS, now=NOW) == NOW - timedelta(seconds=LAG)


def test_a_saved_watermark_is_resumed_verbatim() -> None:
    cursors, stores = _cursors()
    when = NOW - timedelta(hours=3)
    stores[ACTIVITIES].value = Checkpoint(timestamp=when, id="act_9")
    assert cursors.window_start(ACTIVITIES, now=NOW) == when


def test_advance_persists_a_timestamp_and_never_a_page_token() -> None:
    cursors, stores = _cursors()
    cursors.advance(ACTIVITIES, timestamp=NOW, id="act_9", drained=True)
    assert stores[ACTIVITIES].saves == [Checkpoint(timestamp=NOW, id="act_9")]


def test_a_truncated_drain_does_not_advance_the_watermark() -> None:
    # The listing is newest-first, so the sessions a cap leaves out are the
    # oldest. Advancing past them loses them permanently.
    cursors, stores = _cursors()
    before = cursors.window_start(SESSIONS, now=NOW)
    cursors.advance(SESSIONS, timestamp=NOW, drained=False)
    assert stores[SESSIONS].saves == []
    assert cursors.window_start(SESSIONS, now=NOW + timedelta(minutes=5)) >= before


def test_the_three_feeds_are_independent() -> None:
    cursors, stores = _cursors()
    cursors.advance(CHATS, timestamp=NOW, drained=True)
    assert stores[ACTIVITIES].saves == []
    assert stores[SESSIONS].saves == []


def test_window_age_is_reported_for_the_backlog_alarm() -> None:
    cursors, stores = _cursors()
    stores[SESSIONS].value = Checkpoint(timestamp=NOW - timedelta(hours=2), id=None)
    assert cursors.window_age_seconds(SESSIONS, now=NOW) == 7200
