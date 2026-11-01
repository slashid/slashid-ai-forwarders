from __future__ import annotations

import asyncio
import contextlib

from slashid_openai_forwarder.compliance import WorkspaceUser
from slashid_openai_forwarder.directory import UserDirectory


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Listing:
    def __init__(self, *pages: list[WorkspaceUser] | Exception) -> None:
        self.pages = list(pages)
        self.calls = 0

    async def __call__(self) -> list[WorkspaceUser]:
        self.calls += 1
        page = self.pages[min(self.calls, len(self.pages)) - 1]
        if isinstance(page, Exception):
            raise page
        return page


ALICE = WorkspaceUser(id="user-a", email="Alice@Example.com")
BOB = WorkspaceUser(id="user-b", email="bob@example.com")


def _directory(listing: _Listing, clock: _Clock) -> UserDirectory:
    return UserDirectory(listing, ttl_seconds=600, miss_refresh_seconds=60, monotonic=clock)


async def test_an_email_resolves_case_insensitively_from_one_listing() -> None:
    clock, listing = _Clock(), _Listing([ALICE])
    directory = _directory(listing, clock)
    assert await directory.user_id("alice@example.com") == "user-a"
    assert await directory.user_id("ALICE@example.com") == "user-a"
    assert listing.calls == 1


async def test_a_miss_lists_again_but_not_on_every_prompt() -> None:
    clock, listing = _Clock(), _Listing([ALICE], [ALICE, BOB])
    directory = _directory(listing, clock)
    assert await directory.user_id("bob@example.com") is None
    assert await directory.user_id("bob@example.com") is None
    assert listing.calls == 1
    clock.now += 61
    assert await directory.user_id("bob@example.com") == "user-b"
    assert listing.calls == 2


async def test_a_failed_listing_keeps_the_map_and_backs_off() -> None:
    clock, listing = _Clock(), _Listing([ALICE], RuntimeError("503"))
    directory = _directory(listing, clock)
    assert await directory.user_id("alice@example.com") == "user-a"
    clock.now += 601
    assert await directory.user_id("alice@example.com") == "user-a"
    assert await directory.user_id("alice@example.com") == "user-a"
    assert listing.calls == 2


async def test_a_caller_that_gives_up_does_not_cancel_the_listing() -> None:
    gate = asyncio.Event()

    async def slow() -> list[WorkspaceUser]:
        await gate.wait()
        return [ALICE]

    directory = UserDirectory(slow, ttl_seconds=600, miss_refresh_seconds=60)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(directory.user_id("alice@example.com"), timeout=0.01)
    gate.set()
    assert await directory.user_id("alice@example.com") == "user-a"
