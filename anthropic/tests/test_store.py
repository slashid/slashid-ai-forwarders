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
from slashid_anthropic_forwarder.store import FirestorePendingStore, Seen
from tests.fake_firestore import FakeFirestore

NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)
JOIN_WAIT = timedelta(hours=1)
ADDRESS = "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


def a_store() -> tuple[FirestorePendingStore, FakeFirestore]:
    client = FakeFirestore()
    return (
        FirestorePendingStore(client=client, collection="anthropic_pending", join_wait=JOIN_WAIT),
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
