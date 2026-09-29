"""The soft join: unanimity, abstention, and the emit it cannot do."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from slashid_ai_forwarder_core.events import AIAccessedFile

from slashid_anthropic_forwarder.compliance.client import ComplianceClient, chat_session_id
from slashid_anthropic_forwarder.compliance.responses import soft_join_uploads
from slashid_anthropic_forwarder.compliance.schema import Chat
from slashid_anthropic_forwarder.compliance.softjoin import SoftMatch, soft_join
from slashid_anthropic_forwarder.record import (
    COMPLIANCE_SOFT,
    HOOK,
    PARSED_AS_JOINED,
    PendingRecord,
    from_document,
    open_fields,
    to_event,
)
from slashid_anthropic_forwarder.store import Outcome, Seen
from slashid_anthropic_forwarder.store.gcp import FirestorePendingStore
from tests.compliance_fixtures import body, transport
from tests.test_pending import Sink, a_store, an_event, fake
from tests.test_pending import config as a_config

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def a_chat(n: int) -> Chat:
    return Chat.model_validate(body(f"chat_messages_{n}.json"))


async def seed_rounds(
    store: FirestorePendingStore, chat: Chat, *, conversation: str | None = None
) -> list[str]:
    """One live record per user message, timestamped as the frame that
    round would have carried. Left unpushed, so every one is a candidate."""
    addresses: list[str] = []
    key = conversation if conversation is not None else chat_session_id(chat)
    for i, message in enumerate(chat.chat_messages):
        if message.role != "user":
            continue
        address = f"hook:{chat.id}-{i}"
        event = an_event(address).model_copy(
            update={"conversation_id": key, "timestamp": message.created_at}
        )
        await store.upsert(address, open_fields(event, webhook_id=f"msg_{i}", contributed=HOOK), ())
        addresses.append(address)
    return addresses


async def digests_on(store: FirestorePendingStore, address: str) -> list[dict[str, Any]]:
    record = await store.claim(address, timedelta(minutes=1), owner="test")
    assert record is not None
    return record.file_digests


async def run(chat: Chat, store: Any, **over: Any) -> list[SoftMatch]:
    client, _ = transport()
    return await soft_join_uploads(
        chat,
        client=ComplianceClient(client, api_key="k"),
        store=store,
        config=a_config(
            **{
                "compliance_key": "sk-ant-api01-x",
                "organization_uuid": "org-1",
                **over,
            }
        ),
    )


async def test_a_unique_candidate_is_enriched() -> None:
    # chat_messages_1: three uploads on the opening message, answered by a
    # text-only turn no address can reach. The next user message is 94 s
    # away, so the window holds exactly one record.
    chat = a_chat(1)
    store = a_store()
    addresses = await seed_rounds(store, chat)
    assert await run(chat, store) == [SoftMatch.ENRICHED]
    digests = await digests_on(store, addresses[0])
    assert [d["name"] for d in digests] == [
        "guiaSADT.pdf",
        "WhatsApp Image 2026-09-02 at 17.07.21.jpeg",
        "maria.txt",
    ]
    assert digests[2]["content_hashes"]["md5"] == "9ae4c5f2489fadc563c6f747d6298fe4"


async def test_several_candidates_abstain() -> None:
    # chat_messages_3 uploads the same image twice, 10 s apart. The first
    # round is the hard path's; the second sees both records inside the
    # window and declines — this is the measurement that killed
    # nearest-wins, since the nearer of the two is not obviously the right
    # one and a wrong pick is silent.
    chat = a_chat(3)
    store = a_store()
    addresses = await seed_rounds(store, chat)
    assert await run(chat, store) == [SoftMatch.AMBIGUOUS]
    for address in addresses:
        assert await digests_on(store, address) == []


async def test_zero_candidates_abstain() -> None:
    chat = a_chat(1)
    store = a_store()
    assert await run(chat, store) == [SoftMatch.NONE]
    assert fake(store).docs == {}


async def test_a_soft_match_never_creates_a_record() -> None:
    # The whole safety argument: a wrong match costs wrong hashes on one
    # event, never a second or a misattributed invocation. Here the only
    # candidates are in another conversation, which is the case a
    # conversation-blind rule would have got wrong.
    chat = a_chat(1)
    store = a_store()
    await seed_rounds(store, chat, conversation="00000000-0000-4000-8000-00000000ffff")
    before = set(fake(store).docs)
    assert await run(chat, store) == [SoftMatch.NONE]
    assert set(fake(store).docs) == before
    assert await store.seen(chat.id) is Seen.ABSENT


async def test_an_enriched_record_is_not_pushed_and_stays_live() -> None:
    chat = a_chat(1)
    store, sink = a_store(), Sink()
    addresses = await seed_rounds(store, chat)
    assert await run(chat, store) == [SoftMatch.ENRICHED]
    assert sink.bodies == []
    assert await store.seen(addresses[0]) is Seen.LIVE


async def test_the_window_decides_and_widening_it_loses_the_match() -> None:
    # chat_messages_2's upload sits 45 s after the round before it: unique
    # at ±15 s, ambiguous at ±60 s. The knob is not a tolerance to relax.
    chat = a_chat(2)
    store = a_store()
    await seed_rounds(store, chat)
    assert await run(chat, store) == [SoftMatch.ENRICHED]
    wider = a_store()
    await seed_rounds(wider, chat)
    assert await run(chat, wider, soft_join_window_seconds=60) == [SoftMatch.AMBIGUOUS]


async def test_a_round_the_hard_path_covers_is_never_offered() -> None:
    # chat_messages_3's first upload is answered by a turn carrying a
    # tool_use, so `_walk` already delivered its digests under the shared
    # address. Only the second round reaches the soft join.
    chat = a_chat(3)
    uploads = [m for m in chat.chat_messages if m.files]
    assert len(uploads) == 2
    assert len(await run(chat, a_store())) == 1


async def test_the_target_protocol_is_the_bound() -> None:
    # Two methods and nothing else: a double with no `upsert`, no sink and
    # no config satisfies `soft_join` completely, which is what makes
    # "may enrich, may never emit" a property of the signature.
    seen: list[tuple[str, dict[str, Any]]] = []

    class OnlyEnrich:
        async def nearby(
            self,
            conversation_id: str,
            *,
            at: datetime,
            window: timedelta,
            limit: int = 25,
        ) -> list[PendingRecord]:
            seen.append(("nearby", {"conversation": conversation_id}))
            return [PendingRecord(address="toolu_01A", event={}, deadline=at, next_attempt_at=at)]

        async def complete(
            self,
            address: str,
            fields: dict[str, Any],
            clears: Any = (),
            *,
            now: datetime | None = None,
        ) -> Outcome:
            seen.append(("complete", {"address": address, "fields": fields}))
            return Outcome(stored=True, ready=True)

    match = await soft_join(
        OnlyEnrich(),
        conversation_id="c",
        at=NOW,
        digests=[AIAccessedFile(name="maria.txt", provenance="attachment")],
        window=timedelta(seconds=15),
    )
    assert match is SoftMatch.ENRICHED
    assert [name for name, _ in seen] == ["nearby", "complete"]
    assert seen[1][1]["fields"]["contributed"].values == (COMPLIANCE_SOFT,)


async def test_a_soft_joined_record_reaches_the_wire_as_joined() -> None:
    chat = a_chat(1)
    store = a_store()
    addresses = await seed_rounds(store, chat)
    await run(chat, store)
    document = fake(store).docs[f"anthropic_pending/{addresses[0]}"][0]
    event = to_event(from_document(addresses[0], document))
    assert event.parsed_as == PARSED_AS_JOINED
    assert [f.name for f in event.accessed_files or []][-1] == "maria.txt"
