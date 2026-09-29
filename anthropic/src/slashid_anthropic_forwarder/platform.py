"""The Anthropic forwarder's backends, built for the cloud it runs on.

``main.py`` asks this module for everything stateful and never imports a
cloud SDK itself. The generic pieces (checkpoints, the tick lease, blobs,
the scheduler's identity) come from the platform ``config.platform``
names; the pending store is this adapter's own interface, so its cloud
implementations are chosen here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from slashid_ai_forwarder_core import platform as platforms
from slashid_ai_forwarder_core.platform import BlobSink, Platform, SchedulerAuth, TickLease
from slashid_ai_forwarder_core.platform.gcp import GcpPlatform

from .compliance.checkpoint import ACTIVITIES, CHATS, SESSIONS, Cursors, FeedCursor
from .config import Config
from .store import FirestorePendingStore, PendingStore


@dataclass(frozen=True)
class Backends:
    store: PendingStore
    lease: TickLease
    cursors: Cursors
    tick_auth: SchedulerAuth
    capture: BlobSink | None


def build_backends(config: Config) -> Backends:
    platform = platforms.get(
        config.platform,
        project=config.gcp_project_id,
        firestore_database=config.firestore_database,
    )

    def cursor(feed: str) -> FeedCursor:
        return FeedCursor(
            platform.checkpoint_store(collection=config.checkpoint_collection, document=feed),
            name=feed,
            poll_lag_seconds=config.poll_lag_seconds,
        )

    return Backends(
        store=_pending_store(platform, config),
        # In the pending collection, which no query of the store matches it in.
        lease=platform.tick_lease(collection=config.pending_collection, document="tick"),
        cursors=Cursors(
            activities=cursor(ACTIVITIES), chats=cursor(CHATS), sessions=cursor(SESSIONS)
        ),
        tick_auth=platform.scheduler_auth(
            principal=config.tick_service_account, audience=config.tick_audience
        ),
        capture=platform.blob_sink(config.capture_bucket) if config.capture_bucket else None,
    )


def _pending_store(platform: Platform, config: Config) -> PendingStore:
    """Both durations are passed: ``tombstone_ttl`` defaults to two hours in
    the store, and leaving it there would make
    ``SLASHID_TOMBSTONE_TTL_SECONDS`` an environment variable with no effect
    on anything."""
    if isinstance(platform, GcpPlatform):
        return FirestorePendingStore(
            client=platform.firestore_async,
            collection=config.pending_collection,
            join_wait=timedelta(seconds=config.join_wait_seconds),
            tombstone_ttl=timedelta(seconds=config.tombstone_ttl_seconds),
        )
    raise NotImplementedError(f"no pending store for {type(platform).__name__}")
