"""Cloud Function polling loop — vendor-neutral pipeline composition.

Runs once per Cloud Scheduler tick (Cloud Scheduler → Pub/Sub topic →
Cloud Function 2nd gen):

  1. For each configured event source: fetch a bounded batch of new
     ``EventEnvelope`` objects past the source's checkpoint (each
     source owns its own checkpoint store).
  2. Build ``AIInvocationObservedV1`` events + POST them to the SlashID
     NHI sink.
  3. On full-batch success: commit the source's next checkpoint. On
     any failure inside a source's fetch/push: log and continue — the
     source's checkpoint stays put and the next tick reprocesses
     (server dedupes on request_id).

Sources run independently with per-source try/except so one source's
failure does not block the others.

Function concurrency = 1 (Pub/Sub subscription
``maxConcurrentDispatches=1``) so checkpoint reads and writes race
nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence

import httpx
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    EventEnvelope,
    build_event_from_normalized,
    redact_for_logging,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.sink import push_invocations

from .config import Config
from .event_source import EventSource

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


def _log_event(event: AIInvocationObservedV1) -> None:
    """Log the event as JSON, redacted for ops-side consumption."""
    redacted = redact_for_logging(event.model_dump(mode="json", exclude_none=True))
    log.info("event: %s", json.dumps(redacted, separators=(",", ":")))


async def _push_envelopes(envelopes: Sequence[EventEnvelope], config: Config) -> int:
    """Build events from envelopes and push them to the SlashID sink.

    Sources deliver fully-formed ``EventEnvelope`` objects — the
    handler no longer does per-source entry-to-envelope conversion.
    Events are built with an empty ``NormalizedInvocation`` — sparse
    fields (input/output/stop_reason/used_tools/etc.) stay null on the
    wire; sources that want them populated stitch them into the
    envelope before returning it.
    """
    if not envelopes:
        return 0

    empty_normalized = NormalizedInvocation()
    events = [
        await build_event_from_normalized(empty_normalized, envelope, config=config)
        for envelope in envelopes
    ]
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
    sources: Sequence[EventSource],
    config: Config,
) -> dict[str, int]:
    """One scheduler-triggered polling cycle.

    Runs each source independently: fetch → push → commit per source,
    wrapped in try/except so one source's failure does not block the
    others. Exceptions are logged with full traceback; that source's
    checkpoint stays put and the next tick reprocesses (server dedupes
    on request_id).
    """
    total_events = 0
    total_envelopes = 0
    for source in sources:
        try:
            envelopes, next_checkpoint = source.fetch()
            if envelopes:
                event_count = asyncio.run(_push_envelopes(envelopes, config))
                total_events += event_count
                total_envelopes += len(envelopes)
            if next_checkpoint is not None:
                source.commit(next_checkpoint)
        except Exception:
            log.exception("source %s failed this tick", type(source).__name__)
    return {"events_pushed": total_events, "envelopes_seen": total_envelopes}
