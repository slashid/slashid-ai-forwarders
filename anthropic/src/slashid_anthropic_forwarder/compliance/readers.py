"""One reader pass.

Reader B runs first: it reads the transcripts, which is where the model
of a denied conversation is, and Reader A has no ``model`` of its own.
Both run before the flush, because a reader's ``complete`` can make a
record ready and a record that became ready on this tick should go out
on this tick.

Each reader is isolated. They are independent sources, a 429 on one feed
must not cost what the other already landed, and Cloud Scheduler retries
a failed tick — which would re-run a reader that already advanced its
watermark. So a reader failure is a log line and a missing counter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from ..config import Config
from ..store import PendingStore
from .checkpoint import Cursors
from .client import ComplianceClient
from .denials import read_denials
from .responses import read_responses

log = logging.getLogger(__name__)


async def run_readers(
    *,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    cursors: Cursors | None,
    compliance: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Both readers, in order, each isolated. Counters for the tick's log.

    ``compliance`` overrides the transport the API calls go over; by
    default they share the tick's client, since every header that client
    sends is per request.
    """
    if not config.compliance_enabled or not config.compliance_key:
        return {}
    if cursors is None:
        raise ValueError("compliance is enabled but no checkpoint store was wired")
    moment = now or datetime.now(UTC)
    client = ComplianceClient(
        compliance or http,
        api_key=config.compliance_key,
        timeout=config.compliance_timeout_seconds,
    )
    counters: dict[str, int] = {}

    models: dict[str, str] = {}
    try:
        responses = await read_responses(
            client, store=store, cursors=cursors, config=config, http=http, now=moment
        )
    except Exception:
        log.exception("compliance: reader B failed; reader A continues with no model map")
    else:
        models = responses.models
        counters |= {
            "responses_emitted": responses.emitted,
            "responses_enriched": responses.enriched,
            "responses_unjoinable": responses.unjoinable,
            # The soft join's own tally, kept apart from `enriched`: one
            # is a match on an id both sources minted, the other a match
            # on a conversation and a clock.
            "responses_soft_enriched": responses.soft_enriched,
            "responses_soft_abstained": responses.soft_abstained,
            "responses_session_failed": responses.session_failed,
            "responses_chat_failed": responses.chat_failed,
            "responses_before_horizon": responses.before_horizon,
            "responses_max_first_seen_lag_s": round(responses.max_first_seen_lag_s),
            "responses_budget_exhausted": int(responses.budget_exhausted),
            "responses_emitted_from_tail": responses.emitted_from_tail,
        }

    try:
        denials = await read_denials(
            client,
            store=store,
            cursors=cursors,
            config=config,
            http=http,
            models=models,
            now=moment,
        )
    except Exception:
        log.exception("compliance: reader A failed")
    else:
        counters |= {
            "denials_handled": denials.handled,
            "denials_emitted": denials.emitted,
            "denials_completed": denials.completed,
        }
    return counters
