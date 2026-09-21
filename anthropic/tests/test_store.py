"""FirestorePendingStore — the write path: upsert, complete, seen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from slashid_ai_forwarder_core.events import (
    AIInvocationContent,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)

from slashid_anthropic_forwarder.record import (
    FILE_DIGESTS,
    HOOK,
    MAX_EVENT_BYTES,
    Append,
    PendingRecord,
    event_fields,
)
from slashid_anthropic_forwarder.store import (
    FirestorePendingStore,
    PendingStore,
    Retirement,
    Seen,
    TickLease,
)
from tests.fake_firestore import FakeFirestore

NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)
JOIN_WAIT = timedelta(hours=1)
ADDRESS = "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


def a_store(
    *, join_wait: timedelta = JOIN_WAIT, tombstone_ttl: timedelta = timedelta(hours=2)
) -> tuple[FirestorePendingStore, FakeFirestore]:
    client = FakeFirestore()
    return (
        FirestorePendingStore(
            client=client,
            collection="anthropic_pending",
            join_wait=join_wait,
            tombstone_ttl=tombstone_ttl,
        ),
        client,
    )


def an_event(**over: object) -> dict[str, object]:
    return {"request_id": ADDRESS, "timestamp": "2026-09-20T23:08:20+00:00"} | over


async def test_upsert_creates_and_reports_readiness() -> None:
    store, client = a_store()
    outcome = await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert (outcome.stored, outcome.created, outcome.ready) == (True, True, True)
    stored = client.docs["anthropic_pending/" + ADDRESS][0]
    assert stored["deadline"] == NOW + JOIN_WAIT
    assert stored["next_attempt_at"] == NOW + JOIN_WAIT
    # Written explicitly: Firestore's IS_NULL filter does not match a
    # document that lacks the field, and `due` depends on it.
    assert stored["tombstoned_at"] is None


async def test_expectations_make_a_record_unready() -> None:
    store, _ = a_store()
    outcome = await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    assert outcome.stored and not outcome.ready


async def test_a_second_delivery_merges_and_never_moves_the_deadline() -> None:
    """43 of 239 measured invocations were revealed by more than one
    delivery; a second one must not extend the wait."""
    store, client = a_store()
    await store.upsert(
        ADDRESS, {"event": an_event(), "webhook_ids": Append(("msg_a",))}, (), now=NOW
    )
    outcome = await store.upsert(
        ADDRESS,
        {"event": an_event(model={"id": "claude-opus-5"}), "webhook_ids": Append(("msg_b",))},
        (),
        now=NOW + timedelta(minutes=20),
    )
    assert outcome.stored and not outcome.created and outcome.ready
    stored = client.docs["anthropic_pending/" + ADDRESS][0]
    assert stored["deadline"] == NOW + JOIN_WAIT
    # Append, not replace: Reader A matches a denial against any of them.
    assert stored["webhook_ids"] == ["msg_a", "msg_b"]
    assert stored["event"]["model"] == {"id": "claude-opus-5"}


async def test_upsert_is_a_no_op_on_a_tombstoned_address() -> None:
    store, client = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, "pushed", now=NOW)
    outcome = await store.upsert(ADDRESS, {"event": an_event(model={"id": "x"})}, (), now=NOW)
    assert not outcome.stored and not outcome.ready
    assert "model" not in client.docs["anthropic_pending/" + ADDRESS][0]["event"]


async def test_complete_never_creates() -> None:
    """A record that does not exist was never opened by a frame; inventing
    one here would resurrect an invocation that was already pushed."""
    store, client = a_store()
    outcome = await store.complete(ADDRESS, {"event": an_event()}, ())
    assert not outcome.stored
    assert client.docs == {}


async def test_complete_is_a_no_op_on_a_tombstoned_address() -> None:
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    await store.retire(ADDRESS, "pushed", now=NOW)
    assert not (await store.complete(ADDRESS, {"event": {}}, (FILE_DIGESTS,))).stored


async def test_complete_merges_into_the_event_without_replacing_it() -> None:
    store, client = a_store()
    await store.upsert(ADDRESS, {"event": an_event(input={"byte_length": 9})}, (), now=NOW)
    await store.complete(ADDRESS, {"event": {"output": {"byte_length": 4}}}, ())
    event = client.docs["anthropic_pending/" + ADDRESS][0]["event"]
    assert event["input"] == {"byte_length": 9} and event["output"] == {"byte_length": 4}


async def test_two_completers_both_land() -> None:
    """Different fields, neither lost — the merge is per field, not per
    document."""
    store, client = a_store()
    opened = {"event": an_event(), "contributed": Append((HOOK,))}
    await store.upsert(ADDRESS, opened, (FILE_DIGESTS,), now=NOW)
    first = await store.complete(ADDRESS, {"event": {"output": {"byte_length": 4}}}, ())
    second = await store.complete(
        ADDRESS,
        {
            "event": {"accessed_files": [{"name": "maria.txt"}]},
            "contributed": Append(("compliance",)),
        },
        (FILE_DIGESTS,),
    )
    assert not first.ready and second.ready
    event = client.docs["anthropic_pending/" + ADDRESS][0]["event"]
    assert event["output"] == {"byte_length": 4}
    assert event["accessed_files"] == [{"name": "maria.txt"}]
    assert client.docs["anthropic_pending/" + ADDRESS][0]["contributed"] == [HOOK, "compliance"]


async def test_a_visit_that_finds_nothing_still_settles_the_record() -> None:
    """Digests are outstanding until a reader visits, not until it finds
    files; otherwise a listing that never materializes waits out the full
    deadline for nothing."""
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    assert (await store.complete(ADDRESS, {}, (FILE_DIGESTS,))).ready


async def test_seen_has_three_states() -> None:
    """Absent is what lets a reader emit standalone."""
    store, _ = a_store()
    assert await store.seen(ADDRESS) is Seen.ABSENT
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert await store.seen(ADDRESS) is Seen.LIVE
    await store.retire(ADDRESS, "pushed", now=NOW)
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_an_oversized_event_is_bounded_before_it_reaches_the_store() -> None:
    """The adapter writes what it is handed; `record.event_fields` is what
    keeps the write under the limit. Asserted here so the division of labour
    is pinned by a test and not only by a docstring."""
    event = AIInvocationObservedV1(
        request_id=ADDRESS,
        timestamp="2026-09-20T23:08:20+00:00",
        identity_details=AnthropicIdentityDetails(user_id="user_01"),
        model=AIModel(id="claude-opus-5"),
        parsed_as="anthropic-inference-hook",
        input=AIInvocationContent(redacted_text="x" * 2_000_000, byte_length=2_000_000),
    )
    store, client = a_store()
    await store.upsert(ADDRESS, event_fields(event), (), now=NOW)
    record: PendingRecord = (await store.due(NOW + timedelta(hours=2), 10))[0]
    assert record.elided
    assert len(str(client.docs["anthropic_pending/" + ADDRESS][0]["event"])) < MAX_EVENT_BYTES


LEASE = timedelta(minutes=5)
TOMBSTONE_TTL = timedelta(hours=2)
PAST_DEADLINE = NOW + JOIN_WAIT + timedelta(minutes=1)


async def test_claim_returns_the_record_as_it_is_now() -> None:
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    await store.complete(ADDRESS, {"event": {"output": {"byte_length": 4}}}, (FILE_DIGESTS,))
    record = await store.claim(ADDRESS, LEASE, owner="tick-1", now=PAST_DEADLINE)
    assert record is not None
    assert record.address == ADDRESS
    assert record.event["output"] == {"byte_length": 4}
    assert record.ready and record.deadline == NOW + JOIN_WAIT


async def test_exactly_one_of_two_claims_wins() -> None:
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    first = await store.claim(ADDRESS, LEASE, owner="completer", now=PAST_DEADLINE)
    second = await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    assert first is not None and second is None


async def test_a_completer_that_merges_before_the_sweep_claims_wins_the_digests() -> None:
    """The race the wait exists for: whoever claims pushes, and what they
    push includes everything merged up to that instant. Note the clock —
    the sweep is locked out for the length of the lease, not forever, and
    what keeps it out afterwards is the tombstone the completer left."""
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    outcome = await store.complete(
        ADDRESS,
        {"event": {"accessed_files": [{"content_hashes": {"md5": "d4 1d"}}]}},
        (FILE_DIGESTS,),
    )
    assert outcome.ready
    completer = await store.claim(ADDRESS, LEASE, owner="completer", now=NOW)
    assert completer is not None
    assert completer.event["accessed_files"][0]["content_hashes"] == {"md5": "d4 1d"}
    assert await store.claim(ADDRESS, LEASE, owner="sweep", now=NOW + LEASE / 2) is None
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW + timedelta(minutes=2))
    assert await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE) is None


async def test_a_completion_after_the_claim_still_lands_but_misses_that_push() -> None:
    """Documented, not accidental: a push is a commitment the terminal will
    not top up, which is why JOIN_WAIT sits beyond normal reader lag."""
    store, client = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    claimed = await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.complete(ADDRESS, {"event": {"accessed_files": [{"name": "late.txt"}]}}, ())
    assert claimed is not None and "accessed_files" not in claimed.event
    assert "accessed_files" in client.docs["anthropic_pending/" + ADDRESS][0]["event"]


async def test_a_claim_that_read_a_stale_snapshot_is_refused_by_the_precondition() -> None:
    """The lease guard is not the arbiter — the compare-and-set is.

    Both pushers read before either writes, so both see no lease and both
    reach the ``update``; only ``last_update_time`` separates them. Every
    other test here returns at the guard three lines earlier, which would
    leave the branch that actually prevents a double push uncovered. The
    interleave is what the fake's ``on_get`` hook exists for."""
    store, client = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)

    async def rival_claims_first(_path: str) -> None:
        client.on_get = None  # one shot; the rival's own read must not recurse
        assert await store.claim(ADDRESS, LEASE, owner="rival", now=PAST_DEADLINE) is not None

    client.on_get = rival_claims_first
    assert await store.claim(ADDRESS, LEASE, owner="loser", now=PAST_DEADLINE) is None
    assert client.docs["anthropic_pending/" + ADDRESS][0]["claim_owner"] == "rival"


async def test_an_expired_lease_can_be_claimed_again() -> None:
    """A crash between claiming and pushing is recovered on a later tick."""
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert await store.claim(ADDRESS, LEASE, owner="crashed", now=PAST_DEADLINE) is not None
    later = PAST_DEADLINE + LEASE + timedelta(seconds=1)
    recovered = await store.claim(ADDRESS, LEASE, owner="next-tick", now=later)
    assert recovered is not None and recovered.claim_owner == "next-tick"


async def test_claim_refuses_an_absent_or_tombstoned_address() -> None:
    store, _ = a_store()
    assert await store.claim(ADDRESS, LEASE, owner="x", now=NOW) is None
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW)
    assert await store.claim(ADDRESS, LEASE, owner="x", now=PAST_DEADLINE) is None


async def test_due_returns_oldest_first_and_honours_its_bound() -> None:
    store, _ = a_store()
    for minutes in (30, 10, 20):
        opened_at = NOW + timedelta(minutes=minutes)
        await store.upsert(f"toolu_{minutes}", {"event": {}}, (), now=opened_at)
    due = await store.due(NOW + timedelta(hours=2), 2)
    assert [r.address for r in due] == ["toolu_10", "toolu_20"]


async def test_due_excludes_the_not_yet_deadlined_the_leased_and_the_tombstoned() -> None:
    store, _ = a_store()
    await store.upsert("toolu_waiting", {"event": {}}, (), now=NOW)
    await store.upsert("toolu_leased", {"event": {}}, (), now=NOW)
    await store.upsert("toolu_gone", {"event": {}}, (), now=NOW)
    await store.claim("toolu_leased", LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire("toolu_gone", Retirement.PUSHED, now=PAST_DEADLINE)
    assert await store.due(NOW + timedelta(minutes=30), 10) == []
    assert [r.address for r in await store.due(PAST_DEADLINE, 10)] == ["toolu_waiting"]


async def test_a_failed_push_releases_the_lease_and_due_returns_it_again() -> None:
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE)
    assert await store.due(PAST_DEADLINE, 10) == []  # backing off, not orphaned
    retried = await store.due(PAST_DEADLINE + timedelta(minutes=2), 10)
    assert [r.address for r in retried] == [ADDRESS]
    assert retried[0].attempts == 1
    assert retried[0].claim_owner is None and retried[0].claim_expires_at is None
    # Backoff grows with attempts, so a persistently failing sink is not a
    # hot loop against the terminal.
    await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE + timedelta(minutes=2))
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE + timedelta(minutes=2))
    assert await store.due(PAST_DEADLINE + timedelta(minutes=3), 10) == []


async def test_retire_superseded_tombstones_without_pushing() -> None:
    """How a successor frame discards the tail record it reconstructed."""
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    tail = "tail:" + "0" * 64
    await store.upsert(tail, {"event": an_event()}, (), now=NOW)
    await store.retire(tail, Retirement.SUPERSEDED, now=NOW)
    assert await store.seen(tail) is Seen.TOMBSTONED
    assert await store.due(PAST_DEADLINE, 10) == []
    # Same clock as a pushed one: a discarded tail suppresses nothing, but
    # letting it expire on a different schedule is one more thing to reason
    # about for no gain.
    stored = client.docs["anthropic_pending/" + tail][0]
    assert stored["tombstone_expires_at"] == NOW + TOMBSTONE_TTL


async def test_retire_is_harmless_on_an_address_that_is_gone() -> None:
    store, _ = a_store()
    await store.retire(ADDRESS, Retirement.FAILED, now=NOW)
    assert await store.seen(ADDRESS) is Seen.ABSENT


async def test_a_tombstone_carries_the_instant_the_ttl_policy_deletes_it() -> None:
    """Firestore deletes a document once the nominated field is in the
    past, so the field holds the expiry, not the moment of tombstoning —
    keying the policy on ``tombstoned_at`` would ask for every tombstone to
    be collected the moment it is written, and the suppression it exists
    for would last only as long as the deletion lag."""
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW)
    stored = client.docs["anthropic_pending/" + ADDRESS][0]
    assert stored["tombstoned_at"] == NOW
    assert stored["tombstone_expires_at"] == NOW + TOMBSTONE_TTL


async def test_a_live_record_is_invisible_to_the_ttl_policy() -> None:
    """A record that has been failing to push for two hours must not be
    deleted out from under the sweep — which is precisely the loss the
    store exists to prevent."""
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE)
    assert "tombstone_expires_at" not in client.docs["anthropic_pending/" + ADDRESS][0]


def test_the_adapter_satisfies_the_port() -> None:
    """Checked by `ty`, not at runtime: the annotation is what makes a
    missing or re-signed method a type error, which is the only thing
    keeping the protocol honest now that six methods exist."""
    port: PendingStore = a_store()[0]
    assert port is not None


TICK = timedelta(minutes=10)


def a_lease(client: FakeFirestore | None = None) -> tuple[TickLease, FakeFirestore]:
    fake = client or FakeFirestore()
    return TickLease(client=fake, collection="anthropic_pending", document="tick"), fake


async def test_one_tick_takes_the_lease_and_the_next_is_turned_away() -> None:
    lease, client = a_lease()
    assert await lease.take(TICK, owner="tick-1", now=NOW) is True
    second, _ = a_lease(client)
    assert await second.take(TICK, owner="tick-2", now=NOW + timedelta(minutes=1)) is False


async def test_the_lease_is_released_at_the_end_of_a_tick() -> None:
    lease, client = a_lease()
    await lease.take(TICK, owner="tick-1", now=NOW)
    await lease.release(owner="tick-1")
    second, _ = a_lease(client)
    assert await second.take(TICK, owner="tick-2", now=NOW + timedelta(minutes=1)) is True


async def test_a_lease_nobody_released_lapses() -> None:
    """A tick that died mid-pass costs one cycle, not the service."""
    lease, client = a_lease()
    await lease.take(TICK, owner="crashed", now=NOW)
    second, _ = a_lease(client)
    assert await second.take(TICK, owner="next", now=NOW + TICK + timedelta(seconds=1)) is True


async def test_releasing_a_lease_someone_else_holds_does_nothing() -> None:
    """After a lapse the lease belongs to the next tick; a straggler
    finishing its own pass must not hand it away."""
    lease, client = a_lease()
    await lease.take(TICK, owner="crashed", now=NOW)
    second, _ = a_lease(client)
    await second.take(TICK, owner="next", now=NOW + TICK + timedelta(seconds=1))
    await lease.release(owner="crashed")
    third, _ = a_lease(client)
    assert await third.take(TICK, owner="third", now=NOW + TICK + timedelta(minutes=1)) is False


async def test_the_lease_document_is_invisible_to_the_sweep() -> None:
    """It shares the collection with the records. It carries no
    ``tombstoned_at``, and an IS_NULL filter does not match a document
    that lacks the field, so `due` never hands it to a pusher."""
    store, client = a_store()
    lease, _ = a_lease(client)
    await lease.take(TICK, owner="tick-1", now=NOW)
    assert await store.due(NOW + timedelta(days=1), 10) == []
