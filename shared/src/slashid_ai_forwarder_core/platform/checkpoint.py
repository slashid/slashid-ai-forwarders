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

    ``timestamp = None`` means "no entries seen yet". What a source does
    with that is the source's decision, and the two in this repo differ:
    Vertex fetches every entry up to its batch bound, while the Anthropic
    compliance readers start at ``now - POLL_LAG`` instead, because a
    backfill there would re-emit the whole retention window.

    Always a timestamp, **never a feed's page token**: those are
    documented as format-unstable, and they paginate within one tick and
    are then discarded.
    """

    timestamp: datetime | None
    id: str | None


class CheckpointStore(Protocol):
    """Load/save the polling watermark; both are awaited.

    ``load`` returns an empty ``Checkpoint(None, None)`` on the very
    first tick (before any prior save) — the source then fetches every
    entry up to the batch bound.
    """

    async def load(self) -> Checkpoint: ...
    async def save(self, checkpoint: Checkpoint) -> None: ...
