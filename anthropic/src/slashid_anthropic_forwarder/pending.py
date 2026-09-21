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
from slashid_ai_forwarder_core.sink import push_invocations

from .config import Config
from .record import PendingRecord, to_event
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
