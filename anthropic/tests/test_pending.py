"""Claiming, pushing, and the deadline flush."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.pending import (
    LEASE,
    MAX_INVALID_ATTEMPTS,
    flush_due,
    push_if_ready,
)
from slashid_anthropic_forwarder.record import HOOK, PARSED_AS_HOOK, open_fields
from slashid_anthropic_forwarder.store import FirestorePendingStore, Seen
from tests.fake_firestore import FakeFirestore

JOIN_WAIT = timedelta(hours=1)
NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)
ADDRESS = "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


def config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.com",
        "push_token": "tok",
        "hook_signing_secret": "whsec_AAAA",
        "gcp_project_id": "proj",
        "max_retries": 0,
    }
    base.update(overrides)
    return Config(**base)


def a_store(**over: Any) -> FirestorePendingStore:
    """A store over a fresh fake. Note `test_store.py` has a helper of the
    same name returning a *tuple*; this one returns the store alone, and
    both are imported around the suite — check which module a call came
    from before copying one."""
    # A zero backoff so `retire(FAILED)` makes the record collectable at
    # once; the real default is 60 s per attempt.
    kwargs: dict[str, Any] = {
        "client": FakeFirestore(),
        "collection": "anthropic_pending",
        "join_wait": JOIN_WAIT,
        "retry_backoff": timedelta(0),
    }
    return FirestorePendingStore(**(kwargs | over))


def fake(store: FirestorePendingStore) -> FakeFirestore:
    """The fake behind a store. The one place that knows the adapter keeps
    its client private, so a rename there is one edit and not twenty."""
    return store._client


def an_event(address: str = ADDRESS) -> AIInvocationObservedV1:
    return AIInvocationObservedV1(
        request_id=address,
        timestamp="2026-09-20T23:08:20+00:00",
        identity_details=AnthropicIdentityDetails(user_id="user_01A"),
        model=AIModel(id="claude-opus-5", provider="anthropic"),
        parsed_as=PARSED_AS_HOOK,
    )


class Sink:
    """Counts pushes and hands back the wire bodies."""

    def __init__(self, *, status: int = 200) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.status = status

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(self.status, json={})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    @property
    def request_ids(self) -> list[str]:
        return [e["request_id"] for body in self.bodies for e in body["events"]]


async def seed(store: FirestorePendingStore, address: str = ADDRESS, *expect: str) -> Any:
    fields = open_fields(an_event(address), webhook_id="msg_1", contributed=HOOK)
    return await store.upsert(address, fields, expect, now=NOW)


async def test_a_record_born_ready_is_pushed_by_its_creator() -> None:
    """Most records are complete on arrival, so nothing ever observes them
    becoming ready; waiting for the deadline would delay nearly every event
    by up to JOIN_WAIT."""
    store, sink = a_store(), Sink()
    outcome = await seed(store)
    async with sink.client() as client:
        pushed = await push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client)
    assert pushed is True
    assert sink.request_ids == [ADDRESS]
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_a_waiting_record_is_left_for_the_reader() -> None:
    store, sink = a_store(), Sink()
    outcome = await seed(store, ADDRESS, "file_digests")
    async with sink.client() as client:
        pushed = await push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client)
    assert pushed is False
    assert sink.bodies == []
    assert await store.seen(ADDRESS) is Seen.LIVE


async def test_exactly_one_of_two_pushers_wins_the_claim() -> None:
    """A completing writer and the deadline sweep can both observe
    readiness. Under first-completed-wins the loser's copy is discarded
    whole, so the arbitration decides whether the digests land — not
    merely how many requests are made.

    The rival is slipped in through the fake's ``on_get`` hook so that both
    pushers read before either writes, which is the only interleaving where
    the compare-and-set decides. Two coroutines under ``asyncio.gather``
    would not reach it: the fake never suspends inside ``claim``, so the
    second reads after the first has written and loses at the lease guard
    three lines earlier."""
    store, sink = a_store(), Sink()
    outcome = await seed(store)
    async with sink.client() as client:

        async def rival_pushes_first(_path: str) -> None:
            fake(store).on_get = None  # one shot; the rival's own read must not recurse
            assert (
                await push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client)
                is True
            )

        fake(store).on_get = rival_pushes_first
        assert (
            await push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client)
            is False
        )
    assert sink.request_ids == [ADDRESS]


async def test_a_push_that_overruns_its_budget_releases_the_claim() -> None:
    """``push_budget_ms`` is declared as the bound on the background push,
    so it has to be one: a sink that never answers would otherwise hold the
    claim for the whole lease."""
    store = a_store()
    hung = 0

    async def never_answers(request: httpx.Request) -> httpx.Response:
        nonlocal hung
        hung += 1
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(never_answers)) as client:
        outcome = await seed(store)
        pushed = await push_if_ready(
            ADDRESS, outcome, store=store, config=config(push_budget_ms=20), client=client
        )
    assert pushed is False and hung == 1
    assert await store.seen(ADDRESS) is Seen.LIVE  # released, not orphaned


async def test_a_record_that_can_never_validate_is_dropped_rather_than_retried_forever() -> None:
    """A stored mapping that is not a whole event fails identically every
    tick, and a live record nothing can push is a document nothing ever
    collects. A transport failure, which may yet succeed, is not capped."""
    store, sink = a_store(), Sink()
    # The wall clock, because `retire(FAILED)` stamps the next attempt with
    # it; a fixed NOW would make the record due exactly once.
    await store.upsert(ADDRESS, {"event": {"request_id": ADDRESS}}, ())
    async with sink.client() as client:
        for _ in range(MAX_INVALID_ATTEMPTS):
            past_deadline = datetime.now(UTC) + JOIN_WAIT * 2
            assert await flush_due(store, config=config(), client=client, now=past_deadline) == 0
    assert sink.bodies == []
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_a_failed_push_releases_the_claim_and_comes_back_due() -> None:
    store, failing, ok = a_store(), Sink(status=500), Sink()
    outcome = await seed(store)
    async with failing.client() as client:
        assert (
            await push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client)
            is False
        )
    assert await store.seen(ADDRESS) is Seen.LIVE  # never landed, so never tombstoned
    later = datetime.now(UTC) + timedelta(seconds=1)
    assert [r.address for r in await store.due(later, 10)] == [ADDRESS]
    async with ok.client() as client:
        assert await flush_due(store, config=config(), client=client, now=later) == 1
    assert ok.request_ids == [ADDRESS]
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_a_claimed_record_is_invisible_to_the_sweep_until_the_lease_lapses() -> None:
    store, sink = a_store(), Sink()
    await seed(store)
    past = NOW + JOIN_WAIT + timedelta(minutes=1)
    assert await store.claim(ADDRESS, LEASE, owner="someone", now=past) is not None
    async with sink.client() as client:
        assert await flush_due(store, config=config(), client=client, now=past + LEASE / 2) == 0
        assert await flush_due(store, config=config(), client=client, now=past + LEASE * 2) == 1


async def test_the_flush_pushes_what_claim_returned_not_what_due_returned() -> None:
    """A completer can merge between the two reads, and a push is a
    commitment the terminal will never top up."""

    class LateCompleter(FirestorePendingStore):
        async def claim(self, address: str, lease: timedelta, **kwargs: Any) -> Any:
            await self.complete(address, {"event": {"request_id": "enriched"}}, ())
            return await super().claim(address, lease, **kwargs)

    store = LateCompleter(
        client=FakeFirestore(), collection="anthropic_pending", join_wait=JOIN_WAIT
    )
    sink = Sink()
    await seed(store)
    async with sink.client() as client:
        await flush_due(store, config=config(), client=client, now=NOW + JOIN_WAIT * 2)
    assert sink.request_ids == ["enriched"]


async def test_the_sweep_is_bounded_per_tick() -> None:
    store, sink = a_store(), Sink()
    for i in range(5):
        await seed(store, f"toolu_{i}")
    async with sink.client() as client:
        flushed = await flush_due(
            store, config=config(max_flushes_per_tick=2), client=client, now=NOW + JOIN_WAIT * 2
        )
    assert flushed == 2
    assert len(sink.request_ids) == 2
