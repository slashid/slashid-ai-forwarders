"""Three watermarks, and the three ways a reader loses data without them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from slashid_ai_forwarder_core.platform import Checkpoint

from slashid_anthropic_forwarder.compliance.checkpoint import (
    ACTIVITIES,
    CHATS,
    SESSIONS,
    Cursors,
    FeedCursor,
)

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
LAG = 120


class _FakeStore:
    def __init__(self) -> None:
        self.value = Checkpoint(None, None)
        self.saves: list[Checkpoint] = []

    async def load(self) -> Checkpoint:
        return self.value

    async def save(self, checkpoint: Checkpoint) -> None:
        self.value = checkpoint
        self.saves.append(checkpoint)


def _cursors() -> tuple[Cursors, dict[str, _FakeStore]]:
    stores = {feed: _FakeStore() for feed in (ACTIVITIES, CHATS, SESSIONS)}
    cursors = Cursors(
        activities=FeedCursor(stores[ACTIVITIES], name=ACTIVITIES, poll_lag_seconds=LAG),
        chats=FeedCursor(stores[CHATS], name=CHATS, poll_lag_seconds=LAG),
        sessions=FeedCursor(stores[SESSIONS], name=SESSIONS, poll_lag_seconds=LAG),
    )
    return cursors, stores


async def test_a_cold_start_is_the_lag_window_not_a_backfill() -> None:
    cursors, _ = _cursors()
    assert await cursors.sessions.window_start(now=NOW) == NOW - timedelta(seconds=LAG)


async def test_a_saved_watermark_is_resumed_verbatim() -> None:
    cursors, stores = _cursors()
    when = NOW - timedelta(hours=3)
    stores[ACTIVITIES].value = Checkpoint(timestamp=when, id="act_9")
    assert await cursors.activities.window_start(now=NOW) == when


async def test_advance_persists_a_timestamp_and_never_a_page_token() -> None:
    cursors, stores = _cursors()
    await cursors.activities.advance(timestamp=NOW, id="act_9", drained=True)
    assert stores[ACTIVITIES].saves == [Checkpoint(timestamp=NOW, id="act_9")]


async def test_a_truncated_drain_does_not_advance_the_watermark() -> None:
    # The listing is newest-first, so the sessions a cap leaves out are the
    # oldest. Advancing past them loses them permanently.
    cursors, stores = _cursors()
    before = await cursors.sessions.window_start(now=NOW)
    await cursors.sessions.advance(timestamp=NOW, drained=False)
    assert stores[SESSIONS].saves == []
    assert await cursors.sessions.window_start(now=NOW + timedelta(minutes=5)) >= before


async def test_the_three_feeds_are_independent() -> None:
    cursors, stores = _cursors()
    await cursors.chats.advance(timestamp=NOW, drained=True)
    assert stores[ACTIVITIES].saves == []
    assert stores[SESSIONS].saves == []


async def test_window_age_is_reported_for_the_backlog_alarm() -> None:
    cursors, stores = _cursors()
    stores[SESSIONS].value = Checkpoint(timestamp=NOW - timedelta(hours=2), id=None)
    assert await cursors.sessions.window_age_seconds(now=NOW) == 7200
