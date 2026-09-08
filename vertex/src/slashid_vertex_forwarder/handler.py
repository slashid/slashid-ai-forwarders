"""Cloud Function polling loop — vendor-neutral pipeline composition.

Runs once per Cloud Scheduler tick (Cloud Scheduler → Pub/Sub topic →
Cloud Function 2nd gen):

  1. Load checkpoint from Firestore.
  2. Fetch a bounded batch of new BQ rows past that checkpoint.
  3. Normalize each row (async, in parallel — Gemini normalizer is
     I/O-only for attachment resolution, which is stub-only in v1).
  4. Build ``AIInvocationObservedV1`` events + POST them to the
     SlashID NHI sink.
  5. On full-batch success: save the last row's checkpoint. On any
     failure: skip the save; next tick reprocesses the same window
     (server dedupes on request_id).

Function concurrency = 1 (Pub/Sub subscription
``maxConcurrentDispatches=1``) so checkpoint reads and writes race
nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    build_event_from_normalized,
    redact_for_logging,
)
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.gemini.normalize import (
    to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.sink import push_invocations

from .checkpoint_store import CheckpointStore
from .config import Config
from .event_envelope import vertex_envelope
from .event_source import Entry, EventSource

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


def _log_event(event: AIInvocationObservedV1) -> None:
    """Log the event as JSON, redacted for ops-side consumption."""
    redacted = redact_for_logging(event.model_dump(mode="json", exclude_none=True))
    log.info("event: %s", json.dumps(redacted, separators=(",", ":")))


async def _run_async(entries: list[Entry], config: Config) -> int:
    """Async half of the tick — normalize, build events, push."""

    async def _prepare(entry: Entry) -> tuple[NormalizedInvocation, Entry]:
        normalized = await to_normalized_invocation(
            entry.request_body, entry.response_body, config=config
        )
        finalize(normalized, config=config)
        return normalized, entry

    prepared = await asyncio.gather(*(_prepare(e) for e in entries))

    async def _build(
        normalized: NormalizedInvocation, entry: Entry
    ) -> AIInvocationObservedV1 | None:
        envelope = vertex_envelope(entry)
        if envelope is None:
            return None
        return await build_event_from_normalized(normalized, envelope, config=config)

    built_or_none = await asyncio.gather(*(_build(n, e) for n, e in prepared))
    events = [e for e in built_or_none if e is not None]
    for e in events:
        _log_event(e)

    timeout = httpx.Timeout(config.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await push_invocations(
            client,
            events,
            endpoint=config.endpoint,
            push_token=config.push_token,
            max_retries=config.max_retries,
        )


def run_tick(
    *,
    source: EventSource,
    checkpoint_store: CheckpointStore,
    config: Config,
) -> dict[str, int]:
    """One scheduler-triggered polling cycle. Sync outer, async inner.

    Called by ``main.handler`` (the functions-framework entrypoint) and
    directly from tests. Returns a small counters dict for CloudWatch-
    style structured logging.
    """
    checkpoint = checkpoint_store.load()
    entries = source.fetch(checkpoint)
    if not entries:
        log.info("no new entries past checkpoint")
        return {"events_pushed": 0, "rows_seen": 0}

    log.info("fetched %d rows past checkpoint", len(entries))
    event_count = asyncio.run(_run_async(entries, config))
    # Advance the checkpoint only on full-batch success. Any exception
    # above propagates before we reach here, and the checkpoint stays
    # put for the next tick to reprocess (server dedupes on request_id).
    checkpoint_store.save(entries[-1].checkpoint)
    return {"events_pushed": event_count, "rows_seen": len(entries)}
