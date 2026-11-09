"""Email → ChatGPT workspace user id.

The hook's token names a user by email; SlashID resolves an OpenAI principal
by its ``user-…`` id. The workspace's own user listing joins the two, so no
server change is needed and the id is the one the sync already put in the
graph.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .compliance import WorkspaceUser

log = logging.getLogger(__name__)


class UserDirectory:
    def __init__(
        self,
        list_users: Callable[[], Awaitable[list[WorkspaceUser]]],
        *,
        ttl_seconds: float,
        miss_refresh_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._list_users = list_users
        self._ttl = ttl_seconds
        self._miss_refresh = miss_refresh_seconds
        self._monotonic = monotonic
        self._by_email: dict[str, str] = {}
        self._loaded_at: float | None = None
        # After a failed listing, none is tried before this instant.
        self._not_before = 0.0
        self._running: asyncio.Task[None] | None = None

    async def user_id(self, email: str) -> str | None:
        """The id for ``email``, or None when the workspace has no such user.

        A stale map is listed again; so is a miss, at most once per
        ``miss_refresh_seconds``, which is how a user added since the last
        listing is found without listing on every unknown prompt. A failed
        listing keeps the map it had and is not retried for as long.

        The caller's deadline may cancel this wait, never the listing: it is
        shielded, so a workspace too large to list inside one verdict budget
        still lands for the next caller.
        """
        email = email.lower()
        if self._due(self._ttl):
            await self._refresh()
        found = self._by_email.get(email)
        if found is None and self._due(self._miss_refresh):
            await self._refresh()
            found = self._by_email.get(email)
        return found

    async def warm(self) -> None:
        if self._due(self._ttl):
            await self._refresh()

    def _due(self, max_age: float) -> bool:
        now = self._monotonic()
        if now < self._not_before:
            return False
        return self._loaded_at is None or now - self._loaded_at >= max_age

    async def _refresh(self) -> None:
        if self._running is None or self._running.done():
            self._running = asyncio.create_task(self._list())
        await asyncio.shield(self._running)

    async def _list(self) -> None:
        try:
            users = await self._list_users()
        except Exception as exc:
            log.warning("directory: listing users failed: %s", exc)
            self._not_before = self._monotonic() + self._miss_refresh
            return
        self._by_email = {u.email.lower(): u.id for u in users if u.email}
        self._loaded_at = self._monotonic()
        log.info("directory: %d users", len(self._by_email))
