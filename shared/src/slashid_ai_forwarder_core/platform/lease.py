"""The guard one tick takes before it does any work. Each cloud's
implementation lives in its own subpackage.

A scheduler can start a second tick while the first is still running:
Cloud Run hands a concurrent request to a second instance, and nothing in
Cloud Scheduler serializes them. Two readers walking the same window
spend the same rate limit twice and both write a checkpoint that has no
precondition, so the watermark can move backwards. The lease is what
makes an overlapping tick a no-op instead.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Protocol


class TickLease(Protocol):
    def hold(self, lease: timedelta) -> AbstractAsyncContextManager[bool]:
        """``async with lease.hold(duration) as held:``. ``held`` is False
        when another tick is running, which is not an error: the body should
        skip, and the next scheduled tick picks the work up. When True, the
        lease is handed back on every way out of the block."""
        ...
