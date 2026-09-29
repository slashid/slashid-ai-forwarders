"""Reader B: one event per newly-produced turn, joinable only, two walks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from slashid_ai_forwarder_core.platform import CheckpointStore
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.address import joinable_address
from slashid_anthropic_forwarder.compliance.checkpoint import Cursors, FeedCursor
from slashid_anthropic_forwarder.compliance.client import ComplianceClient
from slashid_anthropic_forwarder.compliance.responses import (
    ResponseCounters,
    chat_turns,
    produced_runs,
    read_responses,
    response_blocks,
    to_anthropic,
)
from slashid_anthropic_forwarder.compliance.schema import Chat, SessionMessage
from slashid_anthropic_forwarder.hook.frame import PromptFrame, split_transcript
from slashid_anthropic_forwarder.store import FirestorePendingStore, Retirement, Seen
from tests.compliance_fixtures import PAIRED, body, transport
from tests.test_cursors import LAG
from tests.test_cursors import _FakeStore as FakeCheckpoints
from tests.test_pending import Sink, a_store, seed
from tests.test_pending import config as a_config

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
ORG = "11111111-1111-1111-1111-111111111111"


def session_messages(n: int) -> list[SessionMessage]:
    return [SessionMessage.model_validate(m) for m in body(f"session_messages_{n}.json")["data"]]


def addresses(n: int) -> list[str]:
    return [
        a
        for a in (
            joinable_address(to_anthropic(run.messages))
            for run in produced_runs(session_messages(n))
        )
        if a is not None
    ]


def _cursors(**stores: CheckpointStore) -> Cursors:
    """Cursors for a test: every feed gets a fake unless one is named."""
    made = {f: stores.get(f, FakeCheckpoints()) for f in ("activities", "chats", "sessions")}
    return Cursors(**{f: FeedCursor(s, name=f, poll_lag_seconds=LAG) for f, s in made.items()})


def a_reader() -> tuple[ComplianceClient, Cursors]:
    client, _ = transport()
    return ComplianceClient(client, api_key="k"), _cursors()


async def run(
    store: FirestorePendingStore,
    sink: Sink,
    cursors: Cursors | None = None,
    **over: Any,
) -> ResponseCounters:
    client, built = a_reader()
    return await read_responses(
        client,
        store=store,
        cursors=cursors or built,
        # Splatted, not passed as keywords: a case overriding
        # `organization_uuid` would otherwise pass it twice.
        config=a_config(**{"compliance_key": "sk-ant-api01-x", "organization_uuid": ORG, **over}),
        http=sink.client(),
        now=NOW,
    )


@yaml_pytest(filename="test_produced_runs.yaml")
def test_produced_runs(messages: list[dict[str, Any]], expected_models: list[str]) -> None:
    parsed = [SessionMessage.model_validate(m) for m in messages]
    assert [run.model for run in produced_runs(parsed)] == expected_models


def test_the_recorded_synthetic_marker_is_not_a_turn() -> None:
    # Every recorded transcript opens with one, on a *user* message.
    first = session_messages(1)[0]
    assert first.provenance is not None and first.provenance.type == "synthetic_marker"
    assert all(run.index > 0 for run in produced_runs(session_messages(1)))


def test_the_address_matches_the_hook_path_over_the_paired_corpus() -> None:
    """The load-bearing claim, measured: a run's address computed from a
    captured frame equals the one computed from the stored transcript.

    14 frames, 7 of which have an anchor on the consumed round, and all 7
    resolve to an address the transcript walk also produced. The 7 with
    none are opening prompts and tool-free rounds — the class the seam
    closes when a provider-supplied common id ships.
    """
    checked = 0
    for path in sorted(PAIRED.glob("*.json")):
        frame = PromptFrame.model_validate_json(path.read_bytes())
        from_frame = joinable_address(split_transcript(frame).assistant_run)
        if from_frame is None:
            continue  # an opening prompt, or a tool-free run: nothing to join
        session = int(path.name.split("_")[1])
        assert from_frame in addresses(session), path.name
        checked += 1
    assert checked == 7


def test_a_tool_free_run_has_no_address_at_all() -> None:
    # session_messages_3 is one text-only turn. There is no reader-side
    # digest to fall back on, and inventing one would reintroduce the key
    # the 200/302/zero measurement ruled out.
    runs = produced_runs(session_messages(3))
    assert len(runs) == 1
    assert joinable_address(to_anthropic(runs[0].messages)) is None


async def test_an_unjoinable_run_is_left_to_the_hook() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    assert counters.unjoinable >= 1
    assert all(not rid.startswith("hook:") for rid in sink.request_ids)


async def test_a_joinable_turn_the_hook_never_saw_is_emitted_and_tombstoned() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    address = addresses(1)[0]
    assert counters.emitted >= 1
    assert address in sink.request_ids
    pushed = next(e for b in sink.bodies for e in b["events"] if e["request_id"] == address)
    assert pushed["parsed_as"] == "anthropic-compliance"
    assert pushed["stop_reason"] in {"tool_use", "end_turn"}
    assert pushed["tokens"]["input"] == 0
    # Identity is on the listing item: a transcript message carries only
    # type, id, role, created_at, provenance, model and content.
    listed = body("sessions_list.json")["data"][0]["user"]["id"]
    assert pushed["identity_details"]["user_id"] == listed
    assert await store.seen(address) is Seen.TOMBSTONED


async def test_a_live_record_is_enriched_rather_than_emitted() -> None:
    store, sink = a_store(), Sink()
    address = addresses(6)[0]
    await seed(store, address, "file_digests")
    counters = await run(store, sink)
    assert counters.enriched >= 1
    pushed = next(e for b in sink.bodies for e in b["events"] if e["request_id"] == address)
    # `joined` is the proof it went through `complete` with a `contributed`
    # append rather than being opened again as a reader-only record.
    assert pushed["parsed_as"] == "anthropic-joined"
    assert sink.request_ids.count(address) == 1


async def test_a_tombstoned_run_is_not_re_emitted() -> None:
    store, sink = a_store(), Sink()
    address = addresses(1)[0]
    await seed(store, address)
    await store.retire(address, Retirement.PUSHED, now=NOW)
    counters = await run(store, sink)
    assert counters.tombstoned >= 1
    assert address not in sink.request_ids


async def test_the_chats_feed_emits_too() -> None:
    # The walk that did not exist: a chat assistant turn carries its tool
    # results inline and its model on the chat object, and nothing else in
    # the suite would have caught either.
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    chat = Chat.model_validate(body("chat_messages_2.json"))
    turns = chat_turns(chat)
    assert turns and all(t.model == chat.model for t in turns)
    joinable = [a for a in (joinable_address(to_anthropic(t.messages)) for t in turns) if a]
    assert joinable, "chat_messages_2 has tool calls; if not, the fixture changed"
    assert counters.from_chats >= 1
    assert any(rid in sink.request_ids for rid in joinable)
    # And its conversation is the uuid a frame would carry, not the
    # `claude_chat_…` id — the soft join's whole scope rests on it.
    pushed = next(e for b in sink.bodies for e in b["events"] if e["request_id"] in joinable)
    assert chat.href is not None
    assert pushed["conversation_id"] == chat.href.rsplit("/", 1)[-1]


def test_an_inline_tool_result_never_reaches_the_response_union() -> None:
    # Handing a chat assistant message's blocks to AnthropicMessage raises,
    # and inside a tick that takes every reader behind it down.
    chat = Chat.model_validate(body("chat_messages_2.json"))
    turn = next(
        t
        for t in chat_turns(chat)
        if any(block.type == "tool_result" for message in t.messages for block in message.content)
    )
    kinds = {b.type for b in response_blocks(turn.messages[0].content)}
    assert "tool_result" not in kinds
    assert kinds <= {"text", "tool_use", "thinking"}


async def test_a_truncated_drain_leaves_the_sessions_watermark_alone() -> None:
    sessions, chats = FakeCheckpoints(), FakeCheckpoints()
    cursors = _cursors(sessions=sessions, chats=chats)
    await run(a_store(), Sink(), cursors, max_sessions_per_tick=1)
    assert sessions.saves == []
    # The ordered feed is unaffected: it resumes from its own watermark.
    assert chats.saves != []


async def test_another_organizations_conversation_is_skipped() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink, organization_uuid="other")
    assert counters.emitted == 0
    assert counters.skipped_other_org == len(body("sessions_list.json")["data"]) + len(
        body("chats_list.json")["data"]
    )


def test_content_unavailable_and_replayed_turns_are_skipped() -> None:
    # Hand-written: this tenant has no retention policy in force and
    # produced none. A customer with finite retention does, and emitting
    # one would create a contentless invocation.
    messages = [
        SessionMessage.model_validate(m)
        for m in (
            {
                "role": "assistant",
                "model": "claude-opus-5",
                "provenance": {"type": "content_unavailable", "reason": "retention_elapsed"},
                "content": [],
            },
            {
                "role": "assistant",
                "model": "claude-opus-5",
                "provenance": {"type": "client_asserted"},
                "content": [],
            },
        )
    ]
    assert produced_runs(messages) == []


def test_unknown_blocks_survive_translation() -> None:
    translated = to_anthropic(
        [
            SessionMessage.model_validate(
                {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "future"}]}
            )
        ]
    )
    assert len(translated) == 1 and len(translated[0].content) >= 1
