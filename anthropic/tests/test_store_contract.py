"""``PendingStore`` behaviour that must hold on every backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from google.cloud.firestore import AsyncClient as FirestoreAsyncClient
from slashid_ai_forwarder_core.platform.local import create_local_platform

from slashid_anthropic_forwarder.record import Append
from slashid_anthropic_forwarder.store import PendingStore, Retirement, Seen
from slashid_anthropic_forwarder.store.gcp import FirestorePendingStore
from slashid_anthropic_forwarder.store.local import SqlitePendingStore
from tests.fake_firestore import FakeFirestore

NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)
JOIN_WAIT = timedelta(hours=1)
LEASE = timedelta(minutes=5)
TTL = timedelta(hours=2)
PAST_DEADLINE = NOW + JOIN_WAIT + timedelta(minutes=1)
ADDRESS = "toolu_01Dqhr2d1w2UCUqbXhCSGutC"
AWAITING = "file_digests"


@pytest.fixture(params=["firestore", "sqlite"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[PendingStore]:
    if request.param == "firestore":
        yield FirestorePendingStore(
            client=cast(FirestoreAsyncClient, FakeFirestore()),
            collection="anthropic_pending",
            join_wait=JOIN_WAIT,
            tombstone_ttl=TTL,
        )
        return
    async with create_local_platform(None) as platform:
        yield await SqlitePendingStore.open(
            db=platform.sqlite,
            collection="anthropic_pending",
            join_wait=JOIN_WAIT,
            tombstone_ttl=TTL,
        )


def an_event(**over: Any) -> dict[str, Any]:
    return {"request_id": ADDRESS, "timestamp": "2026-09-20T23:08:20+00:00"} | over


async def test_upsert_creates_and_reports_readiness(store: PendingStore) -> None:
    outcome = await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert (outcome.stored, outcome.created, outcome.ready) == (True, True, True)
    waiting = await store.upsert("toolu_w", {"event": an_event()}, (AWAITING,), now=NOW)
    assert waiting.stored and waiting.created and not waiting.ready


async def test_a_second_delivery_merges_and_never_moves_the_deadline(store: PendingStore) -> None:
    await store.upsert(
        ADDRESS, {"event": an_event(), "webhook_ids": Append(("msg_a",))}, (), now=NOW
    )
    outcome = await store.upsert(
        ADDRESS,
        {
            "event": an_event(model={"id": "claude-opus-5"}),
            "webhook_ids": Append(("msg_b", "msg_a")),
        },
        (),
        now=NOW + timedelta(minutes=20),
    )
    assert outcome.stored and not outcome.created and outcome.ready
    record = await store.claim(ADDRESS, LEASE, owner="t", now=PAST_DEADLINE)
    assert record is not None
    assert record.deadline == NOW + JOIN_WAIT
    assert record.webhook_ids == ["msg_a", "msg_b"]
    assert record.event["model"] == {"id": "claude-opus-5"}


async def test_expectations_accumulate_on_merge(store: PendingStore) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    outcome = await store.upsert(ADDRESS, {}, (AWAITING,), now=NOW)
    assert outcome.stored and not outcome.ready


async def test_a_tombstoned_address_is_a_no_op(store: PendingStore) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (AWAITING,), now=NOW)
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW)
    again = await store.upsert(ADDRESS, {"event": an_event(model={"id": "x"})}, (), now=NOW)
    assert not again.stored and not again.ready
    assert not (await store.complete(ADDRESS, {}, (AWAITING,))).stored
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED
    assert await store.claim(ADDRESS, LEASE, owner="t", now=PAST_DEADLINE) is None


async def test_complete_never_creates(store: PendingStore) -> None:
    assert not (await store.complete(ADDRESS, {"event": an_event()}, ())).stored
    assert await store.seen(ADDRESS) is Seen.ABSENT


async def test_complete_merges_maps_per_field_and_replaces_lists(store: PendingStore) -> None:
    await store.upsert(ADDRESS, {"event": an_event(input={"byte_length": 9})}, (), now=NOW)
    await store.complete(ADDRESS, {"event": {"output": {"byte_length": 4}}}, ())
    await store.complete(ADDRESS, {"event": {"accessed_files": [{"name": "a"}]}}, ())
    await store.complete(ADDRESS, {"event": {"accessed_files": [{"name": "b"}]}}, ())
    record = await store.claim(ADDRESS, LEASE, owner="t", now=PAST_DEADLINE)
    assert record is not None
    assert record.event["input"] == {"byte_length": 9}
    assert record.event["output"] == {"byte_length": 4}
    assert record.event["accessed_files"] == [{"name": "b"}]


async def test_two_completers_both_land(store: PendingStore) -> None:
    await store.upsert(
        ADDRESS, {"event": an_event(), "contributed": Append(("hook",))}, (AWAITING,), now=NOW
    )
    first = await store.complete(ADDRESS, {"event": {"output": {"byte_length": 4}}}, ())
    second = await store.complete(ADDRESS, {"contributed": Append(("compliance",))}, (AWAITING,))
    assert not first.ready and second.ready
    record = await store.claim(ADDRESS, LEASE, owner="t", now=PAST_DEADLINE)
    assert record is not None and record.contributed == ["hook", "compliance"]


async def test_a_visit_that_finds_nothing_still_settles_the_record(store: PendingStore) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (AWAITING,), now=NOW)
    assert (await store.complete(ADDRESS, {}, (AWAITING,))).ready


async def test_seen_has_three_states(store: PendingStore) -> None:
    assert await store.seen(ADDRESS) is Seen.ABSENT
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert await store.seen(ADDRESS) is Seen.LIVE
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW)
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_retiring_an_absent_address_tombstones_it(store: PendingStore) -> None:
    """A successor frame supersedes a tail that may never have been written."""
    await store.retire(ADDRESS, Retirement.SUPERSEDED, now=NOW)
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED
    assert not (await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)).stored
    assert await store.due(PAST_DEADLINE, 10) == []


async def test_a_failed_retire_on_an_absent_address_does_nothing(store: PendingStore) -> None:
    await store.retire(ADDRESS, Retirement.FAILED, now=NOW)
    assert await store.seen(ADDRESS) is Seen.ABSENT


async def test_exactly_one_of_two_claims_wins(store: PendingStore) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    first = await store.claim(ADDRESS, LEASE, owner="completer", now=PAST_DEADLINE)
    second = await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    assert first is not None and second is None


async def test_claim_does_not_check_readiness_and_returns_the_record_as_it_is(
    store: PendingStore,
) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (AWAITING,), now=NOW)
    record = await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    assert record is not None and record.address == ADDRESS
    assert not record.ready and record.claim_owner == "sweep"


async def test_a_completion_after_the_claim_lands_but_misses_that_push(
    store: PendingStore,
) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    claimed = await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.complete(ADDRESS, {"event": {"accessed_files": [{"name": "late"}]}}, ())
    assert claimed is not None and "accessed_files" not in claimed.event


async def test_an_expired_lease_can_be_claimed_again(store: PendingStore) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert await store.claim(ADDRESS, LEASE, owner="crashed", now=PAST_DEADLINE) is not None
    later = PAST_DEADLINE + LEASE + timedelta(seconds=1)
    recovered = await store.claim(ADDRESS, LEASE, owner="next", now=later)
    assert recovered is not None and recovered.claim_owner == "next"


async def test_claim_refuses_an_absent_address(store: PendingStore) -> None:
    assert await store.claim(ADDRESS, LEASE, owner="x", now=NOW) is None


async def test_due_returns_oldest_first_and_honours_its_bound(store: PendingStore) -> None:
    for minutes in (30, 10, 20):
        await store.upsert(
            f"toolu_{minutes}", {"event": {}}, (), now=NOW + timedelta(minutes=minutes)
        )
    due = await store.due(NOW + timedelta(hours=3), 2)
    assert [r.address for r in due] == ["toolu_10", "toolu_20"]


async def test_due_excludes_the_waiting_the_leased_and_the_tombstoned(store: PendingStore) -> None:
    for name in ("waiting", "leased", "gone"):
        await store.upsert(f"toolu_{name}", {"event": {}}, (), now=NOW)
    await store.claim("toolu_leased", LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire("toolu_gone", Retirement.PUSHED, now=PAST_DEADLINE)
    assert await store.due(NOW + timedelta(minutes=30), 10) == []
    assert [r.address for r in await store.due(PAST_DEADLINE, 10)] == ["toolu_waiting"]


async def test_a_failed_push_backs_off_and_returns_again(store: PendingStore) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE)
    assert await store.due(PAST_DEADLINE, 10) == []
    retried = await store.due(PAST_DEADLINE + timedelta(minutes=2), 10)
    assert [r.address for r in retried] == [ADDRESS]
    assert retried[0].attempts == 1 and retried[0].claim_owner is None
    second = PAST_DEADLINE + timedelta(minutes=2)
    await store.claim(ADDRESS, LEASE, owner="sweep", now=second)
    await store.retire(ADDRESS, Retirement.FAILED, now=second)
    assert await store.due(PAST_DEADLINE + timedelta(minutes=3), 10) == []


async def test_a_superseded_record_is_tombstoned_without_being_due(store: PendingStore) -> None:
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, Retirement.SUPERSEDED, now=NOW)
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED
    assert await store.due(PAST_DEADLINE, 10) == []


async def test_nearby_returns_live_records_of_one_conversation_inside_the_window(
    store: PendingStore,
) -> None:
    def at(seconds: int, conversation: str = "conv_1", stamp: str | None = None) -> dict[str, Any]:
        when = NOW + timedelta(seconds=seconds)
        return {
            "event": an_event(conversation_id=conversation, timestamp=stamp or when.isoformat())
        }

    await store.upsert("toolu_in", at(5), (), now=NOW)
    await store.upsert("toolu_z", at(-7, stamp="2026-09-20T23:08:13Z"), (), now=NOW)
    await store.upsert("toolu_far", at(120), (), now=NOW)
    await store.upsert("toolu_other", at(0, "conv_2"), (), now=NOW)
    await store.upsert("toolu_dead", at(0), (), now=NOW)
    await store.retire("toolu_dead", Retirement.PUSHED, now=NOW)
    found = await store.nearby("conv_1", at=NOW, window=timedelta(seconds=15))
    assert sorted(r.address for r in found) == ["toolu_in", "toolu_z"]
