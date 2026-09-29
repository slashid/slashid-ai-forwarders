"""Claiming, pushing, and the deadline flush."""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.address import deny_address, tail_address
from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.hook.checks import Decision, Verdict
from slashid_anthropic_forwarder.hook.frame import PromptFrame
from slashid_anthropic_forwarder.pending import (
    LEASE,
    MAX_INVALID_ATTEMPTS,
    flush_due,
    push_if_ready,
    unanswered_round,
    write_from_frame,
)
from slashid_anthropic_forwarder.record import (
    DENIAL_ACTIVITY,
    HOOK,
    PARSED_AS_HOOK,
    open_fields,
)
from slashid_anthropic_forwarder.store import Seen
from slashid_anthropic_forwarder.store.gcp import FirestorePendingStore
from tests.fake_firestore import FakeFirestore

JOIN_WAIT = timedelta(hours=1)
NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)
ADDRESS = "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


def config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.com",
        "push_token": "tok",
        "hook_signing_secret": "whsec_AAAA",
        "project_id": "proj",
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


FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SIGNED_AT = 1758409700
ALLOWED = Decision(composed=Verdict("allow"), answered=Verdict("allow"))
SHADOW_DENY = Decision(composed=Verdict("deny", source="policy"), answered=Verdict("allow"))
BLOCKED = Decision(
    composed=Verdict("deny", source="policy"), answered=Verdict("deny", source="policy")
)
DECISIONS = {"allow": ALLOWED, "shadow_deny": SHADOW_DENY, "blocked": BLOCKED}


def frame(name: str, append: list[dict[str, Any]] | None = None) -> PromptFrame:
    f = PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))
    if not append:
        return f
    extra = [AnthropicRequestMessage.model_validate(m) for m in append]
    return f.model_copy(update={"messages": [*f.messages, *extra]})


def addresses(store: FirestorePendingStore) -> set[str]:
    """Live and tombstoned alike, straight out of the fake's documents.

    Note what that includes: ``retire(SUPERSEDED)`` is a ``set(merge=True)``
    and creates the document when it is absent, so a superseded predecessor
    tail is in here as a tombstone. Partition on ``live`` before counting
    tails."""
    return {path.split("/", 1)[1] for path in fake(store).docs}


def document(store: FirestorePendingStore, address: str) -> dict[str, Any]:
    """The raw stored document. Read through the fake rather than through
    ``claim``, because a record that was pushed is already tombstoned and
    ``claim`` would answer ``None`` for it. The one place that knows the
    fake stores ``(data, update_time)``."""
    return fake(store).docs[f"anthropic_pending/{address}"][0]


def live(store: FirestorePendingStore, address: str) -> bool:
    return document(store, address).get("tombstoned_at") is None


def awaiting(store: FirestorePendingStore, address: str) -> list[str]:
    return sorted(document(store, address).get("awaiting") or [])


async def write(
    f: PromptFrame,
    store: FirestorePendingStore,
    *,
    decision: Decision = ALLOWED,
    webhook_id: str = "msg_1",
    **cfg: Any,
) -> Sink:
    sink = Sink()
    settings = config(**cfg)
    # `main.py` builds the tail event for the verdict and hands the same
    # object on, so the tests do too rather than letting the writer build
    # a second one.
    tail = await unanswered_round(f, webhook_id=webhook_id, signed_at=SIGNED_AT, config=settings)
    async with sink.client() as client:
        await write_from_frame(
            f,
            decision=decision,
            tail_event=tail,
            webhook_id=webhook_id,
            signed_at=SIGNED_AT,
            store=store,
            config=settings,
            client=client,
        )
    return sink


class Expected(BaseModel):
    record: str | None = None  # the previous run's address, or null for none
    tail: bool = True  # a LIVE tail written for the fresh round
    deny: bool = False  # a denial record instead of a tail
    awaiting: list[str] = []  # on the previous run's record
    deny_awaiting: list[str] = []  # on the denial


@yaml_pytest(filename="test_write_from_frame.yaml")
async def test_write_from_frame(
    fixture: str,
    append: list[dict[str, Any]],
    decision: str,
    compliance: bool,
    expected: Expected,
) -> None:
    store = a_store()
    extra = {"compliance_key": "sk-ant-api01-x", "organization_uuid": "org-1"} if compliance else {}
    await write(frame(fixture, append), store, decision=DECISIONS[decision], **extra)
    written = addresses(store)
    tails = {a for a in written if a.startswith("tail:")}
    denials = {a for a in written if a.startswith("deny:")}
    assert written - tails - denials == ({expected.record} if expected.record else set())
    # Live, not merely present: the predecessor tail this frame superseded
    # is a document too, and counting it would make `tail` mean nothing.
    assert any(live(store, a) for a in tails) is expected.tail
    assert bool(denials) is expected.deny
    if expected.record:
        assert awaiting(store, expected.record) == expected.awaiting
    for denial in denials:
        assert awaiting(store, denial) == expected.deny_awaiting


async def test_an_unjoinable_run_is_addressed_on_the_delivery() -> None:
    """No toolu_ id means no key the two sources could agree on, so it gets
    one that needs no agreement and the reader never emits under it."""
    store = a_store()
    await write(frame("frame_after_shadow_deny"), store, webhook_id="msg_7")
    assert "hook:msg_7" in addresses(store)


async def test_an_unjoinable_run_and_a_denial_on_one_frame_stay_separate() -> None:
    """The collision deny_address exists to prevent: both want the delivery
    id, and a merge would leave one record holding the other's event."""
    store = a_store()
    await write(frame("frame_after_shadow_deny"), store, decision=BLOCKED, webhook_id="msg_7")
    assert {"hook:msg_7", "deny:msg_7"} <= addresses(store)
    run = document(store, "hook:msg_7")["event"]
    denial = document(store, "deny:msg_7")["event"]
    assert run.get("stop_reason") != "guardrail_intervened"
    assert denial["stop_reason"] == "guardrail_intervened"
    assert run["input"] != denial["input"]


async def test_the_successor_discards_the_tail_its_predecessor_wrote() -> None:
    store = a_store()
    first = frame("frame_tool_result")
    await write(first, store, webhook_id="msg_1")
    key = tail_address(list(first.messages), first.session_id)
    assert await store.seen(key) is Seen.LIVE

    # The next delivery: the model answered, and the user replied.
    second = frame(
        "frame_tool_result",
        [
            {"role": "assistant", "content": [{"type": "text", "text": "the first line is…"}]},
            {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
        ],
    )
    sink = await write(second, store, webhook_id="msg_2")
    assert await store.seen(key) is Seen.TOMBSTONED
    # The whole list, not `key not in`: a tail's wire id is the delivery
    # id, never its address, so an absent address would prove nothing. The
    # second frame's own run has no tool_use, hence the hook: address.
    assert sink.request_ids == ["hook:msg_2"]  # the superseded tail was not pushed


async def test_a_redundant_delivery_writes_no_second_record() -> None:
    """45 of 492 deliveries carried a transcript already seen: a trailing
    assistant run stays trailing until the model produces a new one."""
    store = a_store()
    f = frame("frame_tool_result")
    await write(f, store, webhook_id="msg_1")
    before = addresses(store)
    await write(f, store, webhook_id="msg_2")
    assert addresses(store) == before


async def test_an_honoured_deny_is_recorded_on_its_own_delivery() -> None:
    """Hook only: nothing will ever complete this record, so it is ready on
    arrival and its writer pushes it."""
    store = a_store()
    sink = await write(frame("frame_tool_result"), store, decision=BLOCKED, webhook_id="msg_9")
    stored = document(store, "deny:msg_9")
    assert stored["event"]["stop_reason"] == "guardrail_intervened"
    assert (stored["verdict"], stored["composed_verdict"]) == ("deny", "deny")
    # The superseded predecessor tail is a tombstone, not a tail this frame
    # wrote; no live tail exists, because the denial took its place.
    assert not any(live(store, a) for a in addresses(store) if a.startswith("tail:"))
    # The wire id is the delivery id, not the address, on a denial as on a
    # tail; the other entry is the previous run's record.
    assert sink.request_ids == [ADDRESS, "msg_9"]
    assert await store.seen("deny:msg_9") is Seen.TOMBSTONED


async def test_a_denial_waits_for_reader_a_when_compliance_is_on() -> None:
    """The completion path is the only route by which the activity's
    authoritative confirmation — and the real client user agent, which no
    frame carries — reach the event. Pushing here would tombstone the
    record within seconds and leave Reader A nothing but a tombstone."""
    store = a_store()
    sink = await write(
        frame("frame_tool_result"),
        store,
        decision=BLOCKED,
        webhook_id="msg_9",
        compliance_key="sk-ant-api01-x",
        organization_uuid="org-1",
    )
    assert awaiting(store, "deny:msg_9") == [DENIAL_ACTIVITY]
    # The previous run's record went; the denial did not.
    assert sink.request_ids == [ADDRESS]
    assert await store.seen("deny:msg_9") is Seen.LIVE
    # And if the reader never arrives, the deadline flush emits it as it
    # stands, which is the hook-only behaviour. `write_from_frame` opens
    # records on the wall clock, so the flush has to look from there.
    flushed = Sink()
    async with flushed.client() as client:
        deadline = datetime.now(UTC) + JOIN_WAIT * 2
        assert await flush_due(store, config=config(), client=client, now=deadline) >= 1
    assert "msg_9" in flushed.request_ids  # the wire id is the delivery id


async def test_a_shadow_deny_is_an_ordinary_invocation() -> None:
    """The request ran, so a successor frame will arrive and settle this
    record normally. Nothing here may claim a block happened."""
    store = a_store()
    await write(frame("frame_tool_result"), store, decision=SHADOW_DENY, webhook_id="msg_9")
    written = addresses(store)
    assert not any(a.startswith("deny:") for a in written)
    assert any(a.startswith("tail:") for a in written)
    stored = document(store, ADDRESS)
    assert (stored["verdict"], stored["composed_verdict"]) == ("allow", "deny")


async def test_a_record_that_needs_nothing_is_pushed_by_the_frame_that_wrote_it() -> None:
    store = a_store()
    sink = await write(frame("frame_tool_result"), store)
    assert ADDRESS in sink.request_ids
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_a_tail_is_never_pushed_on_arrival() -> None:
    """A tail has no expectations, so it is ready the instant it is written
    — and it is the one record that must still wait. Pushing it would emit
    every round twice: once as a tail and once as the previous-run record
    the next frame writes."""
    store = a_store()
    f = frame("frame_tool_result")
    sink = await write(f, store)
    key = tail_address(list(f.messages), f.session_id)
    assert await store.seen(key) is Seen.LIVE
    assert sink.request_ids == [ADDRESS]


async def test_a_denial_the_feed_never_confirms_is_discarded_not_flushed() -> None:
    """Measured in production: our shadow mode off, claude.ai's shadow mode
    on. We answer deny, claude.ai ignores it, the model reads the file, and
    no activity is ever recorded — activities exist only for blocks that
    actually happened. Flushing that record would assert a block that did
    not occur, beside the real invocation record for the same turn.

    The feed is authoritative and its silence past the poll lag is
    evidence, so the record is discarded. Nothing is lost: the invocation
    is reported truthfully by its own content-addressed record."""
    store, sink = a_store(), Sink()
    address = deny_address("msg_phantom")
    await seed(store, address, DENIAL_ACTIVITY)
    past_deadline = datetime.now(UTC) + JOIN_WAIT * 2
    async with sink.client() as client:
        flushed = await flush_due(
            store,
            config=config(compliance_key="sk-ant-x", organization_uuid="org-1"),
            client=client,
            now=past_deadline,
        )
    assert flushed == 0
    assert sink.bodies == []  # nothing claimed a block that never happened
    assert await store.seen(address) is Seen.TOMBSTONED  # retired, not left to retry


async def test_without_a_reader_a_denial_still_flushes() -> None:
    """Hook only: there is no feed to confirm anything, so the operator's
    setting is all there is and the denial must still be reported."""
    store, sink = a_store(), Sink()
    address = deny_address("msg_hookonly")
    await seed(store, address)
    past_deadline = datetime.now(UTC) + JOIN_WAIT * 2
    async with sink.client() as client:
        assert await flush_due(store, config=config(), client=client, now=past_deadline) == 1
    assert sink.request_ids == [address]
