"""Reader A: denials from the activity feed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from slashid_ai_forwarder_core.platform import Checkpoint

from slashid_anthropic_forwarder.address import deny_address
from slashid_anthropic_forwarder.compliance.checkpoint import Cursors, FeedCursor
from slashid_anthropic_forwarder.compliance.client import ComplianceClient
from slashid_anthropic_forwarder.compliance.denials import DenialCounters, read_denials
from slashid_anthropic_forwarder.compliance.schema import Activity
from slashid_anthropic_forwarder.record import DENIAL_ACTIVITY
from slashid_anthropic_forwarder.store import Retirement, Seen
from slashid_anthropic_forwarder.store.gcp import FirestorePendingStore
from tests.compliance_fixtures import body, transport
from tests.test_cursors import LAG
from tests.test_cursors import _FakeStore as FakeCheckpoints
from tests.test_pending import Sink, a_store, seed
from tests.test_pending import config as a_config

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
ORG = "11111111-1111-1111-1111-111111111111"


def the_denial() -> Activity:
    return next(
        Activity.model_validate(row)
        for row in body("activities.json")["data"]
        if row["type"] == "inference_hooks_request_denied"
    )


def a_reader(saved: datetime | None = None) -> tuple[ComplianceClient, Cursors]:
    client, _ = transport()
    feeds = {f: FakeCheckpoints() for f in ("activities", "chats", "sessions")}
    feeds["activities"].value = Checkpoint(saved, None)
    return (
        ComplianceClient(client, api_key="k"),
        Cursors(**{f: FeedCursor(store, name=f) for f, store in feeds.items()}),
    )


async def run(
    store: FirestorePendingStore,
    sink: Sink,
    *,
    models: dict[str, str] | None = None,
    organization_uuid: str = ORG,
    now: datetime = NOW,
    saved: datetime | None = None,
) -> DenialCounters:
    client, cursors = a_reader(saved)
    return await read_denials(
        client,
        store=store,
        cursors=cursors,
        config=a_config(compliance_key="sk-ant-api01-x", organization_uuid=organization_uuid),
        http=sink.client(),
        models=models or {},
        now=now,
    )


async def test_everything_that_is_not_a_denial_is_filtered_out() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    rows = body("activities.json")["data"]
    assert counters.handled == 1
    assert counters.skipped_not_a_denial == len(rows) - 1


async def test_an_unrecorded_denial_is_emitted_standalone_and_tombstoned() -> None:
    # Hook down, or a rollout below 100%: the activity is the whole record,
    # because no surface keeps the content of a denied call.
    store, sink = a_store(), Sink()
    denial = the_denial()
    assert denial.request_id is not None
    address = deny_address(denial.request_id)
    counters = await run(store, sink)
    assert counters.emitted == 1
    assert sink.request_ids == [denial.request_id]
    pushed = sink.bodies[0]["events"][0]
    assert pushed["stop_reason"] == "guardrail_intervened"
    assert pushed["parsed_as"] == "anthropic-compliance"
    assert pushed["identity_details"]["user_id"] == denial.actor.user_id
    assert pushed["user_agent"].startswith("claude-cli/")
    assert pushed["conversation_id"] == denial.conversation_id
    assert pushed["model"]["id"] == "unknown"
    # The retire inside push_if_ready is what stops the next tick
    # re-emitting it.
    assert await store.seen(address) is Seen.TOMBSTONED


async def test_a_live_record_is_completed_rather_than_re_emitted() -> None:
    store, sink = a_store(), Sink()
    denial = the_denial()
    assert denial.request_id is not None
    address = deny_address(denial.request_id)
    # Seeded WITH the expectation Chunk 6 puts on a denial when compliance
    # is on. Seeding none would make this test pass even if `complete`
    # forgot to clear it — the record would be ready either way, and the
    # bug would only show up as denials sitting out the full deadline.
    await seed(store, address, DENIAL_ACTIVITY)
    counters = await run(store, sink)
    assert counters.completed == 1 and counters.emitted == 0
    # Pushed at all means the expectation was cleared.
    pushed = sink.bodies[0]["events"][0]
    assert pushed["stop_reason"] == "guardrail_intervened"
    assert pushed["user_agent"].startswith("claude-cli/")
    # Both sources supplied a field, which is what `joined` means.
    assert pushed["parsed_as"] == "anthropic-joined"


async def test_a_tombstoned_denial_is_left_alone() -> None:
    store, sink = a_store(), Sink()
    denial = the_denial()
    assert denial.request_id is not None
    address = deny_address(denial.request_id)
    await seed(store, address)
    await store.retire(address, Retirement.PUSHED, now=NOW)
    counters = await run(store, sink)
    assert counters.tombstoned == 1
    assert sink.bodies == []


async def test_model_falls_back_to_a_transcript_reader_b_already_read() -> None:
    store, sink = a_store(), Sink()
    denial = the_denial()
    assert denial.conversation_id is not None
    await run(store, sink, models={denial.conversation_id: "claude-opus-5"})
    assert sink.bodies[0]["events"][0]["model"]["id"] == "claude-opus-5"


async def test_another_organizations_activity_is_skipped() -> None:
    # The key reads every linked organization; the binding is per org, and
    # `organization_uuid` is not a query parameter on any feed.
    store, sink = a_store(), Sink()
    counters = await run(store, sink, organization_uuid="other")
    assert counters.handled == 0 and counters.skipped_other_org == 1
    assert sink.bodies == []


async def test_the_watermark_advances_to_the_newest_row_seen() -> None:
    newest = max(row["created_at"] for row in body("activities.json")["data"])
    # A tick whose window opens before the newest recorded row: the
    # watermark lands on that row.
    counters = await run(
        a_store(),
        Sink(),
        now=datetime.fromisoformat(newest) + MINUTE,
        saved=datetime.fromisoformat(newest) - MINUTE,
    )
    assert counters.newest is not None
    assert counters.newest.isoformat().startswith(newest[:19])


async def test_the_watermark_never_moves_backwards() -> None:
    # Every recorded row is hours older than this tick's window, and the
    # start of that window is the floor: a feed with nothing new in it
    # must not drag the watermark back to whatever the last row happened
    # to be, which would re-read the same rows for ever.
    counters = await run(a_store(), Sink(), saved=NOW - timedelta(seconds=LAG))
    assert counters.newest == NOW - timedelta(seconds=LAG)
