"""The polling watermark and the interface that stores it.

Shared because three feeds in ``anthropic/`` need exactly what ``vertex/``
already had. Each cloud's store lives in its own subpackage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Checkpoint:
    """The polling watermark — ``(timestamp, id)`` of the last processed
    entry. Universal across event sources.

    ``timestamp = None`` means "no entries seen yet". A source resolves it
    with ``load_or_start``, which pins the checkpoint to the first tick's
    time: nothing before install is read, so there is no backfill.

    Always a timestamp, **never a feed's page token**: those are
    documented as format-unstable, and they paginate within one tick and
    are then discarded.
    """

    timestamp: datetime | None
    id: str | None


class CheckpointStore(Protocol):
    """Load/save the polling watermark; both are awaited.

    ``load`` returns an empty ``Checkpoint(None, None)`` on the very
    first tick (before any prior save).
    """

    async def load(self) -> Checkpoint: ...
    async def save(self, checkpoint: Checkpoint) -> None: ...


async def load_or_start(
    store: CheckpointStore, *, now: datetime, id: str | None = None
) -> Checkpoint:
    """The saved checkpoint, or on a cold start one pinned to ``now`` and saved.

    Saved at once, not on the first drain: a tick that finds nothing
    commits nothing, and an unsaved start would slide forward with the clock
    and drop what arrived between ticks.
    """
    saved = await store.load()
    if saved.timestamp is not None:
        return saved
    start = Checkpoint(timestamp=now, id=id)
    await store.save(start)
    return start
