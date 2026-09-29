"""Google Cloud: Firestore, Cloud Storage and Cloud Scheduler's OIDC token.

Needs the ``[gcp]`` extra. Every SDK is imported on first use, so a
forwarder that never builds a ``GcpPlatform`` never loads them.
"""

from __future__ import annotations

import asyncio
import logging
from functools import cached_property
from typing import Any

from ..checkpoint import CheckpointStore, FirestoreCheckpointStore
from ..lease import FirestoreTickLease, TickLease
from . import BlobSink, SchedulerAuth

log = logging.getLogger(__name__)


class GcsBlobSink:
    def __init__(self, bucket: str) -> None:
        from google.cloud import storage

        self._bucket = storage.Client().bucket(bucket)

    async def put(self, name: str, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(self._bucket.blob(name).upload_from_string, data, content_type)


class GcpPlatform:
    """One project and one named Firestore database.

    The Firestore clients are exposed as well as the interfaces, because an
    adapter whose own store is Firestore-backed shares this connection
    rather than opening a second one.
    """

    def __init__(self, *, project: str, database: str) -> None:
        self._project = project
        self._database = database

    @cached_property
    def firestore(self) -> Any:
        """Synchronous client: checkpoints are a few single-document reads
        and writes per tick, on a path with no latency budget."""
        from google.cloud import firestore

        return firestore.Client(project=self._project, database=self._database)

    @cached_property
    def firestore_async(self) -> Any:
        from google.cloud import firestore

        return firestore.AsyncClient(project=self._project, database=self._database)

    def checkpoint_store(self, *, collection: str, document: str) -> CheckpointStore:
        return FirestoreCheckpointStore(
            client=self.firestore, collection=collection, document=document
        )

    def tick_lease(self, *, collection: str, document: str) -> TickLease:
        return FirestoreTickLease(
            client=self.firestore_async, collection=collection, document=document
        )

    def blob_sink(self, bucket: str) -> BlobSink:
        return GcsBlobSink(bucket)

    def scheduler_auth(self, *, principal: str | None, audience: str | None) -> SchedulerAuth:
        """Accept only Cloud Scheduler's own token.

        Google signs it, and we check who it names. The audience is
        verified only when one is configured: a service's URI is an
        attribute of the very resource whose environment would carry it, so
        Terraform cannot set it without a cycle, and wherever the service is
        not public Cloud Run has already checked the audience itself. The
        email is the authorization either way.
        """
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        transport = google_requests.Request()

        async def check(token: str) -> bool:
            if not principal:
                log.error("no scheduler principal is configured; refusing every tick")
                return False
            try:
                # Blocking: it fetches and caches Google's signing certificates.
                claims = await asyncio.to_thread(
                    id_token.verify_oauth2_token, token, transport, audience
                )
            except Exception:
                log.warning("tick token rejected")
                return False
            return claims.get("email") == principal and bool(claims.get("email_verified"))

        return check
