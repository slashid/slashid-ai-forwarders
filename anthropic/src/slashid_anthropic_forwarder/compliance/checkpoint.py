"""One watermark per compliance feed, and the three rules that guard it.

A ``(timestamp, id)`` watermark is a resumable cursor only on the two
ordered feeds. On local sessions it is a *window bound*: the listing
cannot be ordered, so the reader re-reads the window each tick and the
pending store's tombstones suppress what it already emitted. Saving
after a partial drain there is not a small inaccuracy — it silently
drops every session the cap left unread.

Each feed gets its own ``FeedCursor`` rather than a shared lookup, so a
reader names the feed it means and cannot reach the wrong one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from slashid_ai_forwarder_core.platform import Checkpoint, CheckpointStore, load_or_start

log = logging.getLogger(__name__)

# Document names in the checkpoint collection. One per feed, and stable:
# renaming one silently starts that feed from a cold start.
ACTIVITIES = "compliance_activities"
CHATS = "compliance_chats"
SESSIONS = "compliance_local_sessions"
FEEDS = (ACTIVITIES, CHATS, SESSIONS)


class FeedCursor:
    """The watermark for one feed, and the guards that keep it honest.

    The store is handed in rather than constructed here, so the same
    class serves the Firestore-backed deployment and the tests.
    """

    def __init__(self, store: CheckpointStore, *, name: str) -> None:
        self._store = store
        self._name = name

    async def window_start(self, *, now: datetime) -> datetime:
        """The lower bound for this tick.

        An empty checkpoint means the credential was just added, and the
        answer is ``now``, saved: a compliance backfill would re-emit the
        whole retention window as standalone events, so it has to be an
        explicit decision someone makes on purpose.
        """
        return (await load_or_start(self._store, now=now)).timestamp or now

    async def advance(self, *, timestamp: datetime, id: str | None = None, drained: bool) -> None:
        """Move the watermark, but only after a drain that finished, and never
        back past where it started: ``now - lag`` on the first tick is older
        than the install."""
        if not drained:
            saved = await self._store.load()
            log.warning(
                "compliance: %s drain incomplete; watermark held at %s (window age %.0fs)",
                self._name,
                saved.timestamp,
                await self.window_age_seconds(now=timestamp),
            )
            return
        saved = await self._store.load()
        if saved.timestamp is not None and timestamp < saved.timestamp:
            return
        await self._store.save(Checkpoint(timestamp=timestamp, id=id))

    async def window_age_seconds(self, *, now: datetime) -> float:
        """How far behind the watermark is. The backlog alarm reads this:
        if arrivals exceed ``MAX_SESSIONS_PER_TICK`` every tick the reader
        never catches up, and the window — not the tick duration — is what
        grows. It must stay under ``TOMBSTONE_TTL``, or tombstones expire
        before the reader re-walks and it re-emits."""
        saved = await self._store.load()
        if saved.timestamp is None:
            return 0.0
        return (now - saved.timestamp).total_seconds()


@dataclass(frozen=True)
class Cursors:
    """The three feeds a tick reads, named rather than keyed.

    Attributes instead of a mapping so a reader cannot ask for a feed
    that does not exist, and so the type checker sees which one it got.
    """

    activities: FeedCursor
    chats: FeedCursor
    sessions: FeedCursor
