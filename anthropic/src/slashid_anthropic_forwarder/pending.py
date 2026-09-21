"""What a frame writes, and what a tick flushes.

The receiver never pushes from the request path: a frame writes records
and returns. The one apparent exception is not one — a write that leaves
a record ready pushes it, because readiness is a state rather than a
transition and a record born ready (under emit-previous, most of them)
would otherwise sit until its deadline with nobody to notice. Every
pusher arbitrates the same way: it claims, and the winner pushes what the
claim handed back.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicRequestMessage,
)
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant
from slashid_ai_forwarder_core.sink import push_invocations

from .address import deny_address, hook_address, joinable_address, tail_address
from .config import Config
from .hook.checks import Decision
from .hook.envelope import partial_event
from .hook.frame import PromptFrame, split_transcript
from .record import DENIAL_ACTIVITY, FILE_DIGESTS, HOOK, PendingRecord, open_fields, to_event
from .store import Outcome, PendingStore, Retirement

log = logging.getLogger(__name__)

# A push is bounded by ``push_budget_ms`` and the sink's own retries, so the
# lease only has to outlive that. It also bounds recovery: a pusher that
# dies between claiming and pushing leaves its record invisible to ``due``
# until the lease lapses, which costs one tick of latency and no event.
LEASE = timedelta(seconds=60)

# The whole tick's lease, taken before any work and released after it. It
# has to outlive a full reader pass plus the flush, since a lapse mid-pass
# lets a second tick walk the same window; a tick that dies costs one
# cycle, because nothing collects an expired lease but the next taker.
TICK_LEASE = timedelta(minutes=10)

# How many times a record that will not validate is allowed back. A
# transport failure retries without limit — a missing audit record is the
# failure this product exists to prevent — but an event that cannot be
# built will not build on the hundredth tick either, and a live record
# nothing can push is a document nothing ever collects. The few attempts
# are for the case a reader merges the missing field in between.
MAX_INVALID_ATTEMPTS = 3

# Diagnostic only. The compare-and-set on the document's update time is
# what actually arbitrates, so two pushers sharing an owner string is a
# readability problem and never a correctness one.
WRITER = "writer"
SWEEP = "sweep"


async def push_claimed(
    record: PendingRecord, *, store: PendingStore, config: Config, client: httpx.AsyncClient
) -> bool:
    """Push a record whose claim this caller holds, then retire it.

    Push then retire, never the reverse: a crash between them re-pushes an
    event the terminal's dedup drops, while retiring first loses it
    outright. Validation happens here rather than at write time because
    this is the one path that can log it and retry — the stored document
    is a serialized mapping, and ``to_event`` is where ``parsed_as`` is
    finally decided from ``contributed`` — and it is the one failure that
    is not retried forever.
    """
    try:
        event = to_event(record)
    except ValueError:
        # Not a transport failure: this record fails identically every
        # tick. A few attempts, in case a reader merges the missing field,
        # and then a tombstone — the alternative is a live document nothing
        # will ever collect.
        attempt = record.attempts + 1
        hopeless = attempt >= MAX_INVALID_ATTEMPTS
        log.exception(
            "record %s did not validate (attempt %d)%s",
            record.address,
            attempt,
            "; discarding it" if hopeless else "",
        )
        await store.retire(record.address, Retirement.SUPERSEDED if hopeless else Retirement.FAILED)
        return False
    try:
        # Bounded by ``push_budget_ms``, which is what that knob is for: a
        # sink that never answers would otherwise hold this claim for the
        # whole lease. ``TimeoutError`` is an ordinary failure here.
        await asyncio.wait_for(
            push_invocations(
                client,
                [event],
                endpoint=config.endpoint,
                push_token=config.push_token,
                max_retries=config.max_retries,
            ),
            timeout=config.push_budget_ms / 1000,
        )
    except Exception:
        log.exception("push failed for %s; releasing the claim", record.address)
        await store.retire(record.address, Retirement.FAILED)
        return False
    await store.retire(record.address, Retirement.PUSHED)
    return True


async def push_if_ready(
    address: str,
    outcome: Outcome,
    *,
    store: PendingStore,
    config: Config,
    client: httpx.AsyncClient,
) -> bool:
    """Push now if this write left the record ready, and won the claim.

    Both ``upsert`` and ``complete`` report readiness for exactly this
    reason. A lost claim is not a failure: it means the sweep, or another
    writer, is already pushing this record.
    """
    if not outcome.ready:
        return False
    record = await store.claim(address, LEASE, owner=WRITER)
    if record is None:
        return False
    return await push_claimed(record, store=store, config=config, client=client)


async def flush_due(
    store: PendingStore,
    *,
    config: Config,
    client: httpx.AsyncClient,
    now: datetime | None = None,
) -> int:
    """Push every record past its deadline. The tick's job, never a request's.

    Expiry pushes rather than deletes: two classes of invocation have no
    compliance counterpart at all — zero-data-retention organizations, and
    the sub-conversations that share a ``session_id`` — so a deleted
    record there is a lost event. A flush emits whatever the record holds,
    which for a tail is usually input alone, and for one that was waiting
    on digests already includes the output a successor frame supplied.
    """
    moment = now or datetime.now(UTC)
    pushed = 0
    for stale in await store.due(moment, config.max_flushes_per_tick):
        # Claim again and push what *that* returns: a completer may have
        # merged the digests since ``due`` took its snapshot, and a push is
        # a commitment that cannot be topped up later.
        record = await store.claim(stale.address, LEASE, owner=SWEEP, now=moment)
        if record is None:
            continue
        if await push_claimed(record, store=store, config=config, client=client):
            pushed += 1
    log.info("flush: %d records pushed", pushed)
    return pushed


# An empty assistant message, appended so that ``partial_event`` attributes
# the *fresh* round instead of the one before it. It answers nothing, and
# whatever the builder derives from it is cleared below.
_NO_ANSWER_YET = AnthropicRequestMessage(role="assistant", content=[])


def _has_attachment(messages: list[AnthropicRequestMessage]) -> bool:
    """Does the last round of ``messages`` carry an upload?

    Blocks, not ``accessed_files``: an image attachment has no extracted
    text and so yields no entry, yet its stored bytes are exactly what a
    reader can digest and a frame cannot.
    """
    return any(
        isinstance(block, AnthropicAttachmentBlock)
        for message in after_last_assistant(messages)
        for block in message.content
    )


async def unanswered_round(
    frame: PromptFrame, *, webhook_id: str, signed_at: int, config: Config
) -> AIInvocationObservedV1 | None:
    """The event for the round nothing has answered yet — the tail event.

    Public because ``main.py`` builds it once per delivery and hands the
    same object to ``decide``, whose preflight body it is, and then to
    ``write_from_frame``: one builder, and the invocation the verdict
    judged is the invocation the record stores.

    ``partial_event`` names the run *before* the trailing one, so it is
    handed the transcript with one empty assistant message appended: the
    whole frame becomes its ``before`` and the fresh round is the round
    attributed. That appended run answers nothing, so ``output`` and
    ``stop_reason`` are cleared — this record is input-only until a
    successor frame or a reader says otherwise. Its wire ``request_id`` is
    the delivery id, per the design's field mapping, which is not its
    address: a tail is filed under a digest of the transcript so its
    successor can find it.

    The append is only sound while the frame ends on a user message, which
    a prompt frame does by construction — it is sent *before* the model
    answers, and all 492 measured deliveries end that way. On one ending
    with an assistant message the appended run would merge into the
    trailing one, and this record would duplicate the previous run's with
    its output stripped. So an empty fresh round records nothing.
    """
    if not split_transcript(frame).fresh:
        return None
    whole = frame.model_copy(update={"messages": [*frame.messages, _NO_ANSWER_YET]})
    event = await partial_event(whole, request_id=webhook_id, signed_at=signed_at, config=config)
    if event is None:
        return None
    return event.model_copy(update={"output": None, "stop_reason": None})


async def write_from_frame(
    frame: PromptFrame,
    *,
    decision: Decision,
    tail_event: AIInvocationObservedV1 | None,
    webhook_id: str,
    signed_at: int,
    store: PendingStore,
    config: Config,
    client: httpx.AsyncClient,
) -> None:
    """Everything one delivery writes. Called from a background task, after
    the verdict has already gone back to Anthropic.

    ``tail_event`` is the fresh round's partial record, built by
    ``unanswered_round`` and already judged by ``decide`` — passed in
    rather than rebuilt so the invocation the verdict saw is the one the
    record stores, and so the transcript is hashed once.
    """
    split = split_transcript(frame)
    verdicts = {
        "verdict": decision.answered.action,
        "composed_verdict": decision.composed.action,
    }

    # 1. The previous run. Emit-previous: this frame carries that run's
    #    input and its output both, so the record is complete on arrival
    #    unless a reader owes it attachment digests.
    anchor = joinable_address(split.assistant_run)
    address = anchor or hook_address(webhook_id)
    event = await partial_event(frame, request_id=address, signed_at=signed_at, config=config)
    if event is not None:
        awaiting = (
            (FILE_DIGESTS,)
            if anchor and config.compliance_enabled and _has_attachment(split.before)
            else ()
        )
        fields = open_fields(event, webhook_id=webhook_id, contributed=HOOK, **verdicts)
        outcome = await store.upsert(address, fields, awaiting)
        await push_if_ready(address, outcome, store=store, config=config, client=client)

    # 2. The predecessor's tail. Dropping this frame's trailing run and the
    #    round after it reconstructs the previous delivery's transcript
    #    exactly, so no per-session pointer is needed — which is just as
    #    well, since one session_id covers a hundred sub-conversations.
    #    Retiring an address that was never written is not a mistake: it
    #    leaves a tombstone that makes an out-of-order predecessor's own
    #    upsert a no-op.
    if split.before:
        await store.retire(tail_address(split.before, frame.session_id), Retirement.SUPERSEDED)

    # 3. This frame's own fresh round, as the verdict already saw it.
    #    Written on every frame: without it a session's last round has no
    #    successor to report it, and the reader never emits unjoinable
    #    runs, so nothing else ever would.
    fresh = tail_event
    if fresh is None:
        return
    if not decision.blocked:
        # A shadow-mode deny lands here too: the request ran, a successor
        # frame will arrive, and this tail is discarded by it. Note the
        # missing ``push_if_ready``: a tail has no expectations, so it is
        # ready at once and is the one record that must still wait.
        # Pushing it would emit every round twice — once as a tail, once
        # as the previous-run record the next frame writes.
        await store.upsert(
            tail_address(list(frame.messages), frame.session_id),
            open_fields(fresh, webhook_id=webhook_id, contributed=HOOK, **verdicts),
            (),
        )
        return
    # An honoured deny produces no response and so no successor frame:
    # nothing will ever supersede this. ``guardrail_intervened`` comes from
    # the verdict that actually went back and is stamped now — a flush an
    # hour later could not tell what shadow mode was set to at this moment.
    #
    # It waits for Reader A wherever one exists: the activity is the
    # authoritative confirmation that the block happened, and it carries
    # the real client user agent, which no frame does. Pushing here would
    # tombstone the record within seconds and leave the reader nothing to
    # complete. Hook only, nothing could ever clear the expectation, so it
    # is not seeded and this pushes like any other ready record; either
    # way the deadline flush emits whatever the record holds.
    denial = fresh.model_copy(update={"stop_reason": "guardrail_intervened"})
    key = deny_address(webhook_id)
    outcome = await store.upsert(
        key,
        open_fields(denial, webhook_id=webhook_id, contributed=HOOK, **verdicts),
        (DENIAL_ACTIVITY,) if config.compliance_enabled else (),
    )
    await push_if_ready(key, outcome, store=store, config=config, client=client)
