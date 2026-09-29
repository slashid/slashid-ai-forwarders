"""Reader B — responses, from the stored transcripts.

One invocation per newly-produced assistant turn, and only for a run
this side **can address**. ``joinable_address`` is the single place that
decides, and nothing in this module tests a run's shape: joinability is
a **seam**, not a permanent rule. A provider-supplied invocation id on
both surfaces is the fix, and when it ships that function prefers it,
stops answering ``None``, and what is emitted, enriched and left alone
widens here with no edit.

``None`` is never a licence to invent a second-best key. A digest over
the transcript prefix is exactly what the measurement ruled out (200
keys frame-side, 302 here, zero in common, and still zero with every
text block removed; the stored transcript prepends a synthetic marker,
carries turns from before capture began, and includes sub-agent turns
no frame shows). A run this side cannot address belongs to the hook,
which already reported it under a delivery id no reader can compute.

Two walks, because the feeds are different shapes:

* a **local session** marks a produced turn with ``model`` and no
  ``provenance``; its tool results arrive in the next user message.
* a **chat** has no per-message model — the chat object carries it — and
  keeps ``tool_result`` blocks *inside* the assistant message. Those
  blocks are not in the response-side union, so handing them to
  ``AnthropicMessage`` raises, and inside a tick that takes down every
  reader behind it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
    EventEnvelope,
    build_event_from_normalized,
)
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    message_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicRequestMessage,
    AnthropicToolUseBlock,
)
from slashid_ai_forwarder_core.normalize.normalized.tools import build_tools_declared

from ..address import joinable_address
from ..config import Config

# The accessed-files recipe is the spine's: a file a verdict allowed must
# not be recorded under a different digest, so this imports the hook's
# function rather than deriving a second one.
from ..hook.envelope import accessed_files_for
from ..pending import push_if_ready
from ..record import COMPLIANCE, FILE_DIGESTS, PARSED_AS_COMPLIANCE, Append, event_fields
from ..store import PendingStore, Seen
from .attachments import files_from_listing, listed_files
from .checkpoint import Cursors
from .client import (
    ComplianceClient,
    ComplianceError,
    TranscriptTooLong,
    chat_session_id,
    decode_session_id,
)
from .schema import (
    Chat,
    ChatMessage,
    ContentBlock,
    SessionListing,
    SessionMessage,
    TranscriptMessage,
)
from .softjoin import DigestTarget, SoftMatch, soft_join

log = logging.getLogger(__name__)

# Blocks an assistant turn may carry on the response side. A chat keeps
# its tool_results in the same message, and AnthropicMessage will not
# validate one.
_RESPONSE_KINDS = frozenset({"text", "tool_use", "thinking"})


@dataclass
class ProducedRun:
    index: int
    # Typed on the shared base, not on either transcript's class: the two
    # walks produce different message types and everything downstream
    # reads only the role, the content and the clock.
    messages: list[TranscriptMessage]
    model: str


@dataclass
class ResponseCounters:
    emitted: int = 0
    enriched: int = 0
    tombstoned: int = 0
    unjoinable: int = 0
    skipped_other_org: int = 0
    from_chats: int = 0
    dropped_no_identity: int = 0
    # The soft join's own tally, kept apart from `enriched`: one is a
    # match on an id both sources minted, the other on a conversation and
    # a clock.
    soft_enriched: int = 0
    soft_abstained: int = 0
    # Conversations that could not be read this tick. Each holds its feed's
    # watermark, so the next tick reads it again.
    session_failed: int = 0
    chat_failed: int = 0
    # The reader stopped at its time budget this tick.
    budget_exhausted: bool = False
    # Turns emitted with the tail as their input, because the full
    # transcript was past `max_transcript_messages`.
    emitted_from_tail: int = 0
    # Turns older than the tombstone horizon, skipped: see `_walk`.
    before_horizon: int = 0
    # How far the latest-arriving turn trailed its `created_at` when this
    # reader first saw it. Measures how late transcripts become visible,
    # which is what the horizon has to tolerate.
    max_first_seen_lag_s: float = 0.0
    models: dict[str, str] = field(default_factory=dict)


def produced_runs(messages: Sequence[SessionMessage]) -> list[ProducedRun]:
    """Newly-produced turns in a **local session** transcript.

    The marker is ``model`` with no ``provenance``: a replayed
    ``client_asserted`` turn, the ``synthetic_marker`` the client never
    sent and a ``content_unavailable`` turn all carry one, and so does
    anything Anthropic adds later — which is the right default, since a
    turn we cannot classify is not one to emit.
    """
    runs: list[ProducedRun] = []
    for i, message in enumerate(messages):
        if message.role != "assistant" or not message.model:
            continue
        if message.provenance is not None:
            continue
        run: list[TranscriptMessage] = [message]
        for follower in messages[i + 1 :]:
            # One answer can arrive as several assistant messages. A
            # follower joins only when it carries neither a marker of its
            # own nor a provenance — anything marked is a different turn.
            if follower.role != "assistant" or follower.model or follower.provenance:
                break
            run.append(follower)
        runs.append(ProducedRun(index=i, messages=run, model=message.model))
    return runs


def chat_turns(chat: Chat) -> list[ProducedRun]:
    """Produced turns in a **chat**, which are simply its assistant turns.

    A chat transcript is the canonical store rather than a client's
    replay, so there is no history to filter and no per-message marker to
    filter it with: no chat message carries ``model`` or ``provenance``,
    and the model is on the chat object.
    """
    model = chat.model or "unknown"
    turns: list[ProducedRun] = []
    for i, message in enumerate(chat.chat_messages):
        if message.role == "assistant":
            turns.append(ProducedRun(index=i, messages=[message], model=model))
    return turns


def response_blocks(blocks: Sequence[ContentBlock]) -> list[ContentBlock]:
    """The subset of an answer that the response-side union admits.

    A chat's assistant message carries its ``tool_result`` blocks inline,
    beside the ``tool_use`` that asked for them. ``AnthropicMessage`` does
    not model that, so passing them through raises a ``ValidationError``
    mid-tick. The results are not lost from the record — they are in the
    transcript this run is attributed against — only from the *answer*.
    """
    return [block for block in blocks if block.type in _RESPONSE_KINDS]


def to_anthropic(messages: Sequence[TranscriptMessage]) -> list[AnthropicRequestMessage]:
    """Compliance messages into the canonical schema the spine speaks.

    The address must be byte-identical to the hook's for the same run, so
    both sides hand ``joinable_address`` the same type. The request-side
    union admits ``tool_result`` in either role, so a chat's inline
    results survive here; blocks it does not model fall through as
    ``AnthropicUnknownBlock`` and are skipped downstream, never rejected.
    """
    out: list[AnthropicRequestMessage] = []
    for message in messages:
        if message.role not in ("user", "assistant"):
            continue
        out.append(
            AnthropicRequestMessage.model_validate(
                {"role": message.role, "content": _dump(message.content)}
            )
        )
    return out


def _dump(blocks: Sequence[ContentBlock]) -> list[dict[str, Any]]:
    """Compliance blocks back onto the wire the vendor schema reads.

    A round trip rather than a translation: every field either union
    models — a tool id, a name, an input, a result's pairing id — comes
    back out, so an address computed from a transcript stays
    byte-identical to the one the hook computed from a frame.
    """
    return [block.model_dump(mode="json") for block in blocks]


@dataclass
class _Deferred:
    """A turn the hook never saw, waiting for its full transcript."""

    run: ProducedRun
    address: str
    digests: list[AIAccessedFile]
    created: datetime | None


@dataclass
class _SessionWork:
    session_id: str
    conversation_id: str
    user_id: str | None
    surface: str | None
    runs: list[_Deferred]
    # Pass one's read, oldest-first: the fallback input when the full
    # transcript is too long to hold.
    tail: list[SessionMessage]


async def read_responses(
    client: ComplianceClient,
    *,
    store: PendingStore,
    cursors: Cursors,
    config: Config,
    http: httpx.AsyncClient,
    now: datetime,
) -> ResponseCounters:
    """One pass over both conversation feeds, inside one time budget.

    Sessions are read in two passes. The first reads only each
    transcript's tail, back to the tombstone horizon, which is all that
    enriching a turn the hook recorded needs. A turn the hook never saw
    needs the whole conversation as its input, so it is queued, and the
    second pass fetches each such transcript once, after the chats. A
    long session therefore costs its full length only when it has a turn
    to emit, and never before the cheap work is done.

    The budget is what guarantees Reader A and the flush still run: when
    it runs out this returns with every unfinished feed's watermark held,
    and the next tick carries on.
    """
    counters = ResponseCounters()
    lag = timedelta(seconds=config.poll_lag_seconds)
    horizon = _horizon(now, config)
    sessions_done = chats_done = False
    try:
        async with asyncio.timeout(config.response_reader_budget_seconds):
            drain = await client.drain_local_sessions(
                since=cursors.sessions.window_start(now=now), limit=config.max_sessions_per_tick
            )
            work, tails_read = await _read_session_tails(
                drain.sessions,
                horizon=horizon,
                now=now,
                client=client,
                store=store,
                config=config,
                http=http,
                counters=counters,
            )
            chats_done = await _read_chats(
                cursors,
                now=now,
                client=client,
                store=store,
                config=config,
                http=http,
                counters=counters,
            )
            emitted = await _emit_deferred(
                work,
                now=now,
                client=client,
                store=store,
                config=config,
                http=http,
                counters=counters,
            )
            # Only a finished drain may move a window bound whose listing is
            # newest-first: the tail a cap leaves is the oldest.
            sessions_done = drain.complete and tails_read and emitted
    except TimeoutError:
        counters.budget_exhausted = True
        log.warning(
            "compliance: the response reader used its %ss budget; "
            "unfinished feeds hold their watermarks",
            config.response_reader_budget_seconds,
        )
    cursors.sessions.advance(timestamp=now - lag, drained=sessions_done)
    cursors.chats.advance(timestamp=now - lag, drained=chats_done)
    return counters


async def _read_session_tails(
    sessions: Sequence[SessionListing],
    *,
    horizon: datetime,
    now: datetime,
    client: ComplianceClient,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    counters: ResponseCounters,
) -> tuple[list[_SessionWork], bool]:
    """Pass one. One conversation that cannot be read must not cost the
    rest: it is skipped, counted, and holds the watermark."""
    work: list[_SessionWork] = []
    ok = True
    for session in sessions:
        if session.organization_uuid != config.organization_uuid:
            counters.skipped_other_org += 1
            continue
        conversation_id = decode_session_id(session.id) or session.id
        # A message carries no user; the listing item does.
        user_id = _listed_user_id(session)
        deferred: list[_Deferred] = []
        try:
            tail, _ = await client.session_tail(session.id, horizon=horizon)
            await _walk(
                produced_runs(tail),
                now=now,
                messages=tail,
                conversation_id=conversation_id,
                user_id=user_id,
                surface=session.product_surface,
                client=client,
                store=store,
                config=config,
                http=http,
                counters=counters,
                defer=deferred,
            )
        except ComplianceError as exc:
            log.warning("compliance: session %s unread this tick: %s", session.id, exc)
            counters.session_failed += 1
            ok = False
            continue
        if deferred:
            work.append(
                _SessionWork(
                    session.id, conversation_id, user_id, session.product_surface, deferred, tail
                )
            )
    return work, ok


async def _emit_deferred(
    work: Sequence[_SessionWork],
    *,
    now: datetime,
    client: ComplianceClient,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    counters: ResponseCounters,
) -> bool:
    """Pass two: each queued session's full transcript, fetched once."""
    ok = True
    for item in work:
        try:
            full: Sequence[SessionMessage] = await client.session_messages(
                item.session_id, max_messages=config.max_transcript_messages
            )
        except TranscriptTooLong:
            # Held in memory whole, a long enough session takes the instance
            # down with it. The event's input is truncated to
            # `max_content_size` regardless, so the tail is what ships.
            log.warning(
                "compliance: session %s is over %s messages; emitting from its tail",
                item.session_id,
                config.max_transcript_messages,
            )
            full = item.tail
            counters.emitted_from_tail += len(item.runs)
        except ComplianceError as exc:
            log.warning("compliance: session %s unread this tick: %s", item.session_id, exc)
            counters.session_failed += 1
            ok = False
            continue
        position = {m.id: i for i, m in enumerate(full) if m.id}
        for deferred in item.runs:
            first = deferred.run.messages[0].id
            index = position.get(first) if first else None
            if index is None:
                # The tail's turn is not in the full read; retry next tick.
                log.warning(
                    "compliance: session %s turn %s missing from its full transcript",
                    item.session_id,
                    deferred.address,
                )
                ok = False
                continue
            await _emit(
                full,
                run=ProducedRun(
                    index=index, messages=deferred.run.messages, model=deferred.run.model
                ),
                address=deferred.address,
                digests=deferred.digests,
                created=deferred.created,
                now=now,
                conversation_id=item.conversation_id,
                user_id=item.user_id,
                surface=item.surface,
                store=store,
                config=config,
                http=http,
                counters=counters,
            )
    return ok


async def _read_chats(
    cursors: Cursors,
    *,
    now: datetime,
    client: ComplianceClient,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    counters: ResponseCounters,
) -> bool:
    """One call per chat returns it whole, so chats need no second pass."""
    ok = True
    async for listed in client.iter_chats(since=cursors.chats.window_start(now=now)):
        if listed.organization_uuid != config.organization_uuid:
            counters.skipped_other_org += 1
            continue
        try:
            await _read_chat(
                listed,
                now=now,
                client=client,
                store=store,
                config=config,
                http=http,
                counters=counters,
            )
        except ComplianceError as exc:
            log.warning("compliance: chat %s unread this tick: %s", listed.id, exc)
            counters.chat_failed += 1
            ok = False
    return ok


async def _read_chat(
    listed: Chat,
    *,
    now: datetime,
    client: ComplianceClient,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    counters: ResponseCounters,
) -> None:
    chat = await client.chat(listed.id)
    messages = chat.chat_messages
    before = counters.emitted + counters.enriched
    await _walk(
        chat_turns(chat),
        now=now,
        messages=messages,
        # The uuid the chat's `href` ends with, which is what a frame
        # calls `session_id` — three of three, measured. The
        # `claude_chat_…` id appears in no frame, so using it would
        # file one conversation under two identifiers.
        conversation_id=chat_session_id(chat) or chat.id,
        user_id=_listed_user_id(chat) or _listed_user_id(listed),
        surface="claude-ai",
        client=client,
        store=store,
        config=config,
        http=http,
        counters=counters,
    )
    counters.from_chats += (counters.emitted + counters.enriched) - before
    for match in await soft_join_uploads(chat, client=client, store=store, config=config):
        if match is SoftMatch.ENRICHED:
            counters.soft_enriched += 1
        else:
            counters.soft_abstained += 1


# Slack on the tombstone horizon for clock skew between Anthropic's
# `created_at` and our own clock.
_HORIZON_MARGIN = timedelta(minutes=5)


def _horizon(now: datetime, config: Config) -> datetime:
    return now - timedelta(seconds=config.tombstone_ttl_seconds) + _HORIZON_MARGIN


def _created(run: ProducedRun) -> datetime | None:
    raw = run.messages[0].created_at if run.messages else None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _walk(
    runs: Sequence[ProducedRun],
    *,
    now: datetime,
    messages: Sequence[TranscriptMessage],
    conversation_id: str,
    user_id: str | None,
    surface: str | None,
    client: ComplianceClient,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    counters: ResponseCounters,
    defer: list[_Deferred] | None = None,
) -> None:
    """Handle each turn. A turn the hook never saw is emitted, or appended
    to ``defer`` when ``messages`` is only the transcript's tail.
    """
    # A conversation is re-read whole whenever it changes, but a tombstone
    # lives only `tombstone_ttl_seconds`. A turn older than that may have
    # lost its tombstone and would be emitted again, and after the
    # server's 72h dedup expires, ingested again. Such a turn was already
    # handled: watermarks never pass an unfinished pass, and a cold start
    # deliberately does not backfill. Anything newer is still covered by
    # its tombstone, so this only drops turns that arrive later than the
    # TTL, which `max_first_seen_lag_s` measures.
    horizon = _horizon(now, config)
    for run in runs:
        counters.models[conversation_id] = run.model
        created = _created(run)
        if created is not None and created < horizon:
            counters.before_horizon += 1
            continue
        address = joinable_address(to_anthropic(run.messages))
        if address is None:
            # Not addressable from this side, so it is the hook's, under a
            # key no reader can compute. No second-best address on purpose
            # — and no shape test here either: `joinable_address` is the
            # seam, so this branch empties itself when a common id ships.
            counters.unjoinable += 1
            continue
        state = await store.seen(address)
        if state is Seen.TOMBSTONED:
            counters.tombstoned += 1
            continue
        digests = await _digests(messages[: run.index], client=client, config=config)
        if state is Seen.LIVE:
            outcome = await store.complete(
                address,
                {
                    "file_digests": [d.model_dump(mode="json", exclude_none=True) for d in digests],
                    "contributed": Append((COMPLIANCE,)),
                },
                (FILE_DIGESTS,),
            )
            counters.enriched += 1
            await push_if_ready(address, outcome, store=store, config=config, client=http)
            continue
        if defer is not None:
            defer.append(_Deferred(run, address, digests, created))
            continue
        await _emit(
            messages,
            run=run,
            address=address,
            digests=digests,
            created=created,
            now=now,
            conversation_id=conversation_id,
            user_id=user_id,
            surface=surface,
            store=store,
            config=config,
            http=http,
            counters=counters,
        )


async def _emit(
    messages: Sequence[TranscriptMessage],
    *,
    run: ProducedRun,
    address: str,
    digests: Sequence[AIAccessedFile],
    created: datetime | None,
    now: datetime,
    conversation_id: str,
    user_id: str | None,
    surface: str | None,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    counters: ResponseCounters,
) -> None:
    """A turn the hook never saw, as a standalone event. ``messages`` is
    the whole transcript: the event's input is everything before the turn.
    """
    event = await _standalone(
        messages,
        run=run,
        address=address,
        conversation_id=conversation_id,
        user_id=user_id,
        surface=surface,
        digests=digests,
        config=config,
    )
    if event is None:
        counters.dropped_no_identity += 1
        return
    # `event_fields`, not `open_fields`: there is no delivery id on
    # this side, and `webhook_ids` is the list Reader A matches a
    # denial against — putting a `clsm_` id in it would be a lie.
    outcome = await store.upsert(
        address, {**event_fields(event), "contributed": Append((COMPLIANCE,))}, ()
    )
    counters.emitted += 1
    if created is not None:
        # First sighting: an ABSENT address that is now tombstoned is
        # never ABSENT again.
        lag = (now - created).total_seconds()
        log.info("compliance: %s first seen %.0fs after created_at", address, lag)
        counters.max_first_seen_lag_s = max(counters.max_first_seen_lag_s, lag)
    # No expectations, so the record is born ready: this pushes it and
    # the retire inside leaves the tombstone the next tick honours.
    await push_if_ready(address, outcome, store=store, config=config, client=http)


async def soft_join_uploads(
    chat: Chat,
    *,
    client: ComplianceClient,
    store: DigestTarget,
    config: Config,
) -> list[SoftMatch]:
    """Soft-join every upload in this chat the hard path cannot reach.

    Chats only: a local-session message carries no ``files[]``, so there
    is nothing there to join softly. ``store`` is typed as the two-method
    target rather than as the store, so this walk cannot emit either.
    """
    window = timedelta(seconds=config.soft_join_window_seconds)
    conversation = chat_session_id(chat) or ""
    messages = chat.chat_messages
    out: list[SoftMatch] = []
    for i, message in enumerate(messages):
        entries = listed_files(message)
        at = message.at
        if not entries or at is None or _hard_covered(messages, i):
            continue
        out.append(
            await soft_join(
                store,
                conversation_id=conversation,
                at=at,
                digests=await files_from_listing(client, entries, config=config),
                window=window,
            )
        )
    return out


def _hard_covered(messages: Sequence[ChatMessage], index: int) -> bool:
    """Has an addressable run already claimed this message's uploads?

    ``_walk`` attributes a round's files to the run that answered it, so
    the answer is whether the next assistant turn has an address. No tool
    test: this asks ``joinable_address``, like everything else, and the
    branch empties itself the day a common id ships.
    """
    for message in messages[index + 1 :]:
        if message.role != "assistant":
            continue
        return joinable_address(to_anthropic([message])) is not None
    return False


async def _digests(
    before: Sequence[TranscriptMessage], *, client: ComplianceClient, config: Config
) -> list[AIAccessedFile]:
    """Listing-derived entries for the round the run consumed."""
    entries = [f for message in _last_round(before) for f in listed_files(message)]
    return await files_from_listing(client, entries, config=config)


def _last_round(messages: Sequence[TranscriptMessage]) -> list[TranscriptMessage]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            return list(messages[i + 1 :])
    return list(messages)


async def _standalone(
    messages: Sequence[TranscriptMessage],
    *,
    run: ProducedRun,
    address: str,
    conversation_id: str,
    user_id: str | None,
    surface: str | None,
    digests: Sequence[AIAccessedFile],
    config: Config,
) -> AIInvocationObservedV1 | None:
    """The event for a joinable turn the hook never saw.

    Unsampled under a partial rollout, arriving while the receiver was
    down, or a session's final round. Worse than a hook-emitted event —
    10 KB-capped tool blocks, no untruncated digests — and far better
    than nothing; ``parsed_as`` says which it is.
    """
    if not user_id:
        # The server rejects an Anthropic identity with no identifier.
        return None
    before = to_anthropic(messages[: run.index])
    answer = response_blocks([block for message in run.messages for block in message.content])
    response = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": _dump(answer),
            # No surface carries a stop reason, so it is inferred from
            # block shape — exactly as the hook path infers it.
            "stop_reason": "tool_use" if answer and answer[-1].type == "tool_use" else "end_turn",
        }
    )
    normalized = await message_to_normalized_invocation(
        AnthropicRequestBody(messages=before), response, config=config
    )
    files = await accessed_files_for(before, config=config)
    normalized.accessed_files = [
        *[f for f in files if f.provenance != "attachment"],
        *digests,
    ]
    names: dict[str, None] = {}
    for message in [*before, *to_anthropic(run.messages)]:
        for block in message.content:
            if isinstance(block, AnthropicToolUseBlock):
                names.setdefault(block.name)
    tools, servers = build_tools_declared((name, None, None) for name in names)
    normalized.input.tools_declared = tools
    normalized.input.tool_servers = servers
    return await build_event_from_normalized(
        normalized,
        EventEnvelope(
            request_id=address,
            timestamp=run.messages[0].created_at or "",
            identity_details=AnthropicIdentityDetails(user_id=user_id),
            model=AIModel(id=run.model, provider="anthropic", raw_model_id=run.model),
            # Overridden by `to_event` from `contributed`.
            parsed_as=PARSED_AS_COMPLIANCE,
            user_agent=surface,
            conversation_id=conversation_id,
        ),
        config=config,
    )


def _listed_user_id(item: SessionListing | Chat) -> str | None:
    """``user.id`` off a **listing item**, never off a message.

    A transcript message carries only ``type, id, role, created_at,
    provenance, model, content`` — there is no identity in it. That id is
    byte-identical to the frame's ``actor.id``, which keeps a
    reader-emitted event and a hook-emitted one on one graph identity
    instead of forking the same human in two.
    """
    return item.user.id if item.user else None
