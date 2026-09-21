"""Three independent cursors, one per compliance feed.

A ``(timestamp, id)`` watermark is a resumable cursor only on the two
ordered feeds. On local sessions it is a *window bound*: the listing
cannot be ordered, so the reader re-reads the window each tick and the
pending store's tombstones suppress what it already emitted. Saving
after a partial drain there is not a small inaccuracy — it silently
drops every session the cap left unread.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta

from slashid_ai_forwarder_core.checkpoint import Checkpoint, CheckpointStore

log = logging.getLogger(__name__)

ACTIVITIES = "compliance_activities"
CHATS = "compliance_chats"
SESSIONS = "compliance_local_sessions"
FEEDS = (ACTIVITIES, CHATS, SESSIONS)


class Cursors:
    """One ``CheckpointStore`` per feed, keyed by feed name.

    The stores are handed in rather than constructed here so the same
    class serves the Firestore-backed deployment and the tests.
    """

    def __init__(self, stores: Mapping[str, CheckpointStore], *, poll_lag_seconds: int) -> None:
        self._stores = dict(stores)
        self._lag = timedelta(seconds=poll_lag_seconds)

    def window_start(self, feed: str, *, now: datetime) -> datetime:
        """The lower bound for this tick.

        An empty checkpoint means the credential was just added, and the
        answer is **not** Vertex's "fetch everything": a compliance
        backfill would re-emit the whole retention window as standalone
        events. Start one lag window back and let a backfill be an
        explicit decision someone makes on purpose.
        """
        saved = self._stores[feed].load()
        if saved.timestamp is None:
            return now - self._lag
        return saved.timestamp

    def advance(
        self,
        feed: str,
        *,
        timestamp: datetime,
        id: str | None = None,
        drained: bool,
    ) -> None:
        """Move the watermark, but only after a drain that finished."""
        if not drained:
            log.warning(
                "compliance: %s drain incomplete; watermark held at %s (window age %.0fs)",
                feed,
                self._stores[feed].load().timestamp,
                self.window_age_seconds(feed, now=timestamp),
            )
            return
        self._stores[feed].save(Checkpoint(timestamp=timestamp, id=id))

    def window_age_seconds(self, feed: str, *, now: datetime) -> float:
        """How far behind the watermark is. The backlog alarm reads this:
        if arrivals exceed ``MAX_SESSIONS_PER_TICK`` every tick the reader
        never catches up, and the window — not the tick duration — is what
        grows. It must stay under ``TOMBSTONE_TTL``, or tombstones expire
        before the reader re-walks and it re-emits."""
        saved = self._stores[feed].load()
        if saved.timestamp is None:
            return 0.0
        return (now - saved.timestamp).total_seconds()
