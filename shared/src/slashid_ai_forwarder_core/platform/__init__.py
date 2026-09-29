"""What a forwarder needs from the cloud it runs on, behind ports.

An adapter's logic — polling a feed, answering a hook, pushing events —
does not care where it runs. What does is a short list: where watermarks
live, where raw blobs go, and how the scheduler that drives a tick proves
who it is. ``Platform`` names those, and one implementation per cloud
supplies them; ``gcp.GcpPlatform`` is the only one today.

An adapter with state of its own (a pending store, a lease) declares its
own ports next to its code and builds them from the same platform, so
moving an adapter to another cloud means implementing those ports there,
not editing the adapter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from ..checkpoint import CheckpointStore

# Whether a bearer token presented to a tick route belongs to the
# scheduler allowed to drive it.
SchedulerAuth = Callable[[str], Awaitable[bool]]


class BlobSink(Protocol):
    """Write-only object storage: one named object per call."""

    async def put(self, name: str, data: bytes, *, content_type: str) -> None: ...


class Platform(Protocol):
    def checkpoint_store(self, *, collection: str, document: str) -> CheckpointStore: ...

    def blob_sink(self, bucket: str) -> BlobSink: ...

    def scheduler_auth(self, *, principal: str | None, audience: str | None) -> SchedulerAuth:
        """``principal`` is the identity allowed to drive the tick; when it
        is unset every token is refused, so a misconfigured deployment
        fails closed. ``audience`` is checked only when set."""
        ...
