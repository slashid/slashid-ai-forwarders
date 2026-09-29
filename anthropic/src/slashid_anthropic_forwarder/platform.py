"""The Anthropic forwarder's backends, built for the cloud it runs on.

``main.py`` asks this module for everything stateful and never imports a
cloud SDK itself. The generic pieces (checkpoints, blobs, the scheduler's
identity) come from ``slashid_ai_forwarder_core.platform``; the pending
store and the tick lease are this adapter's own interfaces, so their cloud
implementations are built here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from slashid_ai_forwarder_core.lease import TickLease
from slashid_ai_forwarder_core.platform import BlobSink, SchedulerAuth
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
    match config.platform:
        case "gcp":
            return _gcp(config)


def _gcp(config: Config) -> Backends:
    platform = GcpPlatform(
        project=config.gcp_project_id, firestore_database=config.firestore_database
    )

    def cursor(feed: str) -> FeedCursor:
        return FeedCursor(
            platform.checkpoint_store(collection=config.checkpoint_collection, document=feed),
            name=feed,
            poll_lag_seconds=config.poll_lag_seconds,
        )

    return Backends(
        # Both durations are passed: ``tombstone_ttl`` defaults to two hours
        # in the adapter, and leaving it there would make
        # ``SLASHID_TOMBSTONE_TTL_SECONDS`` an environment variable with no
        # effect on anything.
        store=FirestorePendingStore(
            client=platform.firestore_async,
            collection=config.pending_collection,
            join_wait=timedelta(seconds=config.join_wait_seconds),
            tombstone_ttl=timedelta(seconds=config.tombstone_ttl_seconds),
        ),
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
