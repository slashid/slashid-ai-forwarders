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

import logging
from collections.abc import Mapping, Sequence
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
    chat_session_id,
    created_at,
    decode_session_id,
    provenance_type,
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
    messages: list[dict[str, Any]]
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
    models: dict[str, str] = field(default_factory=dict)


def produced_runs(messages: Sequence[Mapping[str, Any]]) -> list[ProducedRun]:
    """Newly-produced turns in a **local session** transcript.

    The marker is ``model`` with no ``provenance``: a replayed
    ``client_asserted`` turn, the ``synthetic_marker`` the client never
    sent and a ``content_unavailable`` turn all carry one, and so does
    anything Anthropic adds later — which is the right default, since a
    turn we cannot classify is not one to emit.
    """
    runs: list[ProducedRun] = []
    for i, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("model"):
            continue
        if provenance_type(message) is not None:
            continue
        run = [dict(message)]
        for follower in messages[i + 1 :]:
            # One answer can arrive as several assistant messages. A
            # follower joins only when it carries neither a marker of its
            # own nor a provenance — anything marked is a different turn.
            if (
                follower.get("role") != "assistant"
                or follower.get("model")
                or follower.get("provenance")
            ):
                break
            run.append(dict(follower))
        runs.append(ProducedRun(index=i, messages=run, model=str(message["model"])))
    return runs


def chat_turns(chat: Mapping[str, Any]) -> list[ProducedRun]:
    """Produced turns in a **chat**, which are simply its assistant turns.

    A chat transcript is the canonical store rather than a client's
    replay, so there is no history to filter and no per-message marker to
    filter it with: no chat message carries ``model`` or ``provenance``,
    and the model is on the chat object.
    """
    model = str(chat.get("model") or "unknown")
    turns: list[ProducedRun] = []
    for i, message in enumerate(chat.get("chat_messages") or []):
        if message.get("role") == "assistant":
            turns.append(ProducedRun(index=i, messages=[dict(message)], model=model))
    return turns


def response_blocks(blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The subset of an answer that the response-side union admits.

    A chat's assistant message carries its ``tool_result`` blocks inline,
    beside the ``tool_use`` that asked for them. ``AnthropicMessage`` does
    not model that, so passing them through raises a ``ValidationError``
    mid-tick. The results are not lost from the record — they are in the
    transcript this run is attributed against — only from the *answer*.
    """
    return [dict(b) for b in blocks if b.get("type") in _RESPONSE_KINDS]


def to_anthropic(messages: Sequence[Mapping[str, Any]]) -> list[AnthropicRequestMessage]:
    """Compliance messages into the canonical schema the spine speaks.

    The address must be byte-identical to the hook's for the same run, so
    both sides hand ``joinable_address`` the same type. The request-side
    union admits ``tool_result`` in either role, so a chat's inline
    results survive here; blocks it does not model fall through as
    ``AnthropicUnknownBlock`` and are skipped downstream, never rejected.
    """
    out: list[AnthropicRequestMessage] = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        out.append(
            AnthropicRequestMessage.model_validate(
                {"role": role, "content": content if isinstance(content, list) else []}
            )
        )
    return out


async def read_responses(
    client: ComplianceClient,
    *,
    store: PendingStore,
    cursors: Cursors,
    config: Config,
    http: httpx.AsyncClient,
    now: datetime,
) -> ResponseCounters:
    """One pass over both conversation feeds."""
    counters = ResponseCounters()
    lag = timedelta(seconds=config.poll_lag_seconds)

    drain = await client.drain_local_sessions(
        since=cursors.sessions.window_start(now=now), limit=config.max_sessions_per_tick
    )
    for session in drain.sessions:
        if session.get("organization_uuid") != config.organization_uuid:
            counters.skipped_other_org += 1
            continue
        session_id = session.get("id", "")
        messages = await client.session_messages(session_id)
        await _walk(
            produced_runs(messages),
            messages=messages,
            conversation_id=decode_session_id(session_id) or session_id,
            # A message carries no user; the listing item does.
            user_id=_listed_user_id(session),
            surface=session.get("product_surface"),
            client=client,
            store=store,
            config=config,
            http=http,
            counters=counters,
        )
    # Only a finished drain may move a window bound whose listing is
    # newest-first: the tail a cap leaves is the oldest.
    cursors.sessions.advance(timestamp=now - lag, drained=drain.complete)

    async for listed in client.iter_chats(since=cursors.chats.window_start(now=now)):
        if listed.get("organization_uuid") != config.organization_uuid:
            counters.skipped_other_org += 1
            continue
        chat = await client.chat(listed.get("id", ""))
        messages = list(chat.get("chat_messages") or [])
        before = counters.emitted + counters.enriched
        await _walk(
            chat_turns(chat),
            messages=messages,
            # The uuid the chat's `href` ends with, which is what a frame
            # calls `session_id` — three of three, measured. The
            # `claude_chat_…` id appears in no frame, so using it would
            # file one conversation under two identifiers.
            conversation_id=chat_session_id(chat) or str(chat.get("id") or ""),
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
    cursors.chats.advance(timestamp=now - lag, drained=True)
    return counters


async def _walk(
    runs: Sequence[ProducedRun],
    *,
    messages: Sequence[Mapping[str, Any]],
    conversation_id: str,
    user_id: str | None,
    surface: str | None,
    client: ComplianceClient,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    counters: ResponseCounters,
) -> None:
    for run in runs:
        counters.models[conversation_id] = run.model
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
            continue
        # `event_fields`, not `open_fields`: there is no delivery id on
        # this side, and `webhook_ids` is the list Reader A matches a
        # denial against — putting a `clsm_` id in it would be a lie.
        outcome = await store.upsert(
            address, {**event_fields(event), "contributed": Append((COMPLIANCE,))}, ()
        )
        counters.emitted += 1
        # No expectations, so the record is born ready: this pushes it and
        # the retire inside leaves the tombstone the next tick honours.
        await push_if_ready(address, outcome, store=store, config=config, client=http)


async def soft_join_uploads(
    chat: Mapping[str, Any],
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
    messages = list(chat.get("chat_messages") or [])
    out: list[SoftMatch] = []
    for i, message in enumerate(messages):
        entries = listed_files(message)
        at = created_at(message)
        if not entries or at is None or _hard_covered(messages, i):
            continue
        digests = await files_from_listing(client, entries, config=config)
        out.append(
            await soft_join(
                store,
                conversation_id=conversation,
                at=at,
                digests=[d.model_dump(mode="json", exclude_none=True) for d in digests],
                window=window,
            )
        )
    return out


def _hard_covered(messages: Sequence[Mapping[str, Any]], index: int) -> bool:
    """Has an addressable run already claimed this message's uploads?

    ``_walk`` attributes a round's files to the run that answered it, so
    the answer is whether the next assistant turn has an address. No tool
    test: this asks ``joinable_address``, like everything else, and the
    branch empties itself the day a common id ships.
    """
    for message in messages[index + 1 :]:
        if message.get("role") != "assistant":
            continue
        return joinable_address(to_anthropic([message])) is not None
    return False


async def _digests(
    before: Sequence[Mapping[str, Any]], *, client: ComplianceClient, config: Config
) -> list[AIAccessedFile]:
    """Listing-derived entries for the round the run consumed."""
    entries = [f for message in _last_round(before) for f in listed_files(message)]
    return await files_from_listing(client, entries, config=config)


def _last_round(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            return list(messages[i + 1 :])
    return list(messages)


async def _standalone(
    messages: Sequence[Mapping[str, Any]],
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
    answer = response_blocks(
        [block for message in run.messages for block in (message.get("content") or [])]
    )
    response = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": answer,
            # No surface carries a stop reason, so it is inferred from
            # block shape — exactly as the hook path infers it.
            "stop_reason": "tool_use"
            if answer and answer[-1]["type"] == "tool_use"
            else "end_turn",
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
            timestamp=str(run.messages[0].get("created_at") or ""),
            identity_details=AnthropicIdentityDetails(user_id=user_id),
            model=AIModel(id=run.model, provider="anthropic", raw_model_id=run.model),
            # Overridden by `to_event` from `contributed`.
            parsed_as=PARSED_AS_COMPLIANCE,
            user_agent=surface,
            conversation_id=conversation_id,
        ),
        config=config,
    )


def _listed_user_id(item: Mapping[str, Any]) -> str | None:
    """``user.id`` off a **listing item**, never off a message.

    A transcript message carries only ``type, id, role, created_at,
    provenance, model, content`` — there is no identity in it. That id is
    byte-identical to the frame's ``actor.id``, which keeps a
    reader-emitted event and a hook-emitted one on one graph identity
    instead of forking the same human in two.
    """
    user = item.get("user")
    if isinstance(user, Mapping) and isinstance(user.get("id"), str):
        return user["id"]
    return None
