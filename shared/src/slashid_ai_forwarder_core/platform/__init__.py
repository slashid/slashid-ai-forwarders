"""What a forwarder needs from the cloud it runs on, behind interfaces.

An adapter's logic — polling a feed, answering a hook, pushing events —
does not care where it runs. What does is a short list: where watermarks
live, how overlapping ticks are kept apart, where raw blobs go, and how
the scheduler that drives a tick proves who it is. ``Platform`` names
those, and one implementation per cloud supplies them from its own
subpackage; ``get`` resolves one by name.

An adapter with state of its own (a pending store, a lease) declares its
own interfaces next to its code and builds them from the same platform,
so moving an adapter to another cloud means implementing those there,
not editing the adapter.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from .checkpoint import Checkpoint, CheckpointStore
from .lease import TickLease

__all__ = [
    "BlobSink",
    "Checkpoint",
    "CheckpointStore",
    "Platform",
    "SchedulerAuth",
    "TickLease",
    "get",
]

# Whether a bearer token presented to a tick route belongs to the
# scheduler allowed to drive it.
SchedulerAuth = Callable[[str], Awaitable[bool]]


class BlobSink(Protocol):
    """Write-only object storage: one named object per call."""

    async def put(self, name: str, data: bytes, *, content_type: str) -> None: ...


class Platform(Protocol):
    def checkpoint_store(self, *, collection: str, document: str) -> CheckpointStore: ...

    def tick_lease(self, *, collection: str, document: str) -> TickLease: ...

    def blob_sink(self, bucket: str) -> BlobSink: ...

    def scheduler_auth(self, *, principal: str | None, audience: str | None) -> SchedulerAuth:
        """``principal`` is the identity allowed to drive the tick; when it
        is unset every token is refused, so a misconfigured deployment
        fails closed. ``audience`` is checked only when set."""
        ...


# Name -> "module:factory". A factory is an async context manager that yields
# the platform and releases whatever it holds when the block ends. Imported
# only when asked for, so resolving one platform never loads another's SDK.
_PLATFORMS = {"gcp": "slashid_ai_forwarder_core.platform.gcp:create_gcp_platform"}


def get(name: str, **options: Any) -> AbstractAsyncContextManager[Platform]:
    """The platform called ``name``, as an async context manager built with
    ``options``: each takes its own, such as ``project`` and
    ``firestore_database`` for ``gcp``."""
    try:
        target = _PLATFORMS[name]
    except KeyError:
        raise ValueError(f"unknown platform {name!r}; known: {sorted(_PLATFORMS)}") from None
    module, _, factory = target.partition(":")
    return getattr(importlib.import_module(module), factory)(**options)
