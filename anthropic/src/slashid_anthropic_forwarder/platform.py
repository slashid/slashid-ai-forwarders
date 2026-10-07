"""The Anthropic forwarder's backends, built for the cloud it runs on.

``main.py`` asks this module for everything stateful and never imports a
cloud SDK itself. The generic pieces (checkpoints, the tick lease, blobs,
the scheduler's identity) come from the platform ``config.platform``
names; the pending store is this adapter's own interface, so its
implementation for that platform, from ``store/``, is chosen here.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from slashid_ai_forwarder_core import platform as platforms
from slashid_ai_forwarder_core.platform import BlobSink, Platform, SchedulerAuth, TickLease

from .compliance.checkpoint import ACTIVITIES, CHATS, SESSIONS, Cursors, FeedCursor
from .config import Config
from .store import PendingStore

APP = "slashid_anthropic_forwarder"


@dataclass(frozen=True)
class Backends:
    store: PendingStore
    lease: TickLease
    cursors: Cursors
    tick_auth: SchedulerAuth
    capture: BlobSink | None


@contextlib.asynccontextmanager
async def open_backends(config: Config) -> AsyncIterator[Backends]:
    """The backends for the configured platform, which stays open while the
    block does. The app's lifespan holds it for the life of the process."""
    async with platforms.get(config.platform, **_options(config)) as platform:
        yield await _backends(platform, config)


def _options(config: Config) -> dict[str, Any]:
    if config.platform == "local":
        # Left out, ``path`` makes the registry use the user data directory
        # for ``app``; ``None`` would mean in memory.
        return {"app": APP, **({"path": config.data_dir} if config.data_dir else {})}
    return {"project": config.project_id, "firestore_database": config.database}


async def _backends(platform: Platform, config: Config) -> Backends:
    def cursor(feed: str) -> FeedCursor:
        return FeedCursor(
            platform.checkpoint_store(collection=config.checkpoint_collection, document=feed),
            name=feed,
        )

    store = await _pending_store(platform, config)
    return Backends(
        store=store,
        # In the pending collection, which no query of the store matches it in.
        lease=platform.tick_lease(collection=config.pending_collection, document="tick"),
        cursors=Cursors(
            activities=cursor(ACTIVITIES), chats=cursor(CHATS), sessions=cursor(SESSIONS)
        ),
        tick_auth=platform.scheduler_auth(
            principal=config.tick_principal, audience=config.tick_audience
        ),
        capture=platform.blob_sink(config.capture_bucket) if config.capture_bucket else None,
    )


async def _pending_store(platform: Platform, config: Config) -> PendingStore:
    """Both durations are passed: ``tombstone_ttl`` defaults to two hours in
    the store, and leaving it there would make
    ``SLASHID_TOMBSTONE_TTL_SECONDS`` an environment variable with no effect
    on anything."""
    if config.platform == "gcp":
        from slashid_ai_forwarder_core.platform.gcp import GcpPlatform

        from .store.gcp import FirestorePendingStore

        assert isinstance(platform, GcpPlatform)
        return FirestorePendingStore(
            client=platform.firestore,
            collection=config.pending_collection,
            join_wait=timedelta(seconds=config.join_wait_seconds),
            tombstone_ttl=timedelta(seconds=config.tombstone_ttl_seconds),
        )
    from slashid_ai_forwarder_core.platform.local import LocalPlatform

    from .store.local import SqlitePendingStore

    assert isinstance(platform, LocalPlatform)
    return await SqlitePendingStore.open(
        db=platform.sqlite,
        collection=config.pending_collection,
        join_wait=timedelta(seconds=config.join_wait_seconds),
        tombstone_ttl=timedelta(seconds=config.tombstone_ttl_seconds),
    )
