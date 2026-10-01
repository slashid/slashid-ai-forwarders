"""Reader A — denials, from the activity feed.

A denied call produces no response and therefore no successor frame, so
its pending record can only be completed by this reader or flushed on
its deadline. Two things make the round trip worth it:

* an activity exists **only when the block was honoured**. A shadow-mode
  deny records nothing, so the feed is the authoritative record of what
  was actually blocked and ``SLASHID_SHADOW_MODE`` stays out of the
  correctness path.
* the activity carries a real client user agent (``claude-cli/2.1.278``)
  that no frame does.

It has no ``model``, so that comes from the conversation's transcript
when Reader B read one this tick, and ``"unknown"`` otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import httpx
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)

from ..address import deny_address
from ..config import Config
from ..pending import push_if_ready
from ..record import (
    COMPLIANCE,
    DENIAL_ACTIVITY,
    PARSED_AS_COMPLIANCE,
    Append,
    open_fields,
)
from ..store import PendingStore, Seen
from .checkpoint import Cursors
from .client import DENIED_ACTIVITY, ComplianceClient
from .schema import Activity

log = logging.getLogger(__name__)


@dataclass
class DenialCounters:
    handled: int = 0
    completed: int = 0
    emitted: int = 0
    tombstoned: int = 0
    skipped_not_a_denial: int = 0
    skipped_other_org: int = 0
    dropped_no_identity: int = 0
    newest: datetime | None = None


async def read_denials(
    client: ComplianceClient,
    *,
    store: PendingStore,
    cursors: Cursors,
    config: Config,
    http: httpx.AsyncClient,
    models: Mapping[str, str],
    now: datetime,
) -> DenialCounters:
    """One pass over the activity feed from the saved watermark."""
    start = await cursors.activities.window_start(now=now)
    counters = DenialCounters()
    newest = start
    last_id: str | None = None
    async for activity in client.iter_activities(since=start):
        if activity.at:
            newest = max(newest, activity.at)
        last_id = activity.id or last_id
        if activity.type != DENIED_ACTIVITY:
            # Our own reads land here as `compliance_api_accessed` — 48 of
            # 68 rows in the measured window — alongside eight other types
            # in the recorded one. Filtering by type keeps them all out
            # without the reader needing to know its own api_key_id.
            counters.skipped_not_a_denial += 1
            continue
        if activity.organization_uuid != config.organization_uuid:
            counters.skipped_other_org += 1
            continue
        await _handle(
            activity,
            store=store,
            config=config,
            http=http,
            models=models,
            counters=counters,
        )
    await cursors.activities.advance(timestamp=newest, id=last_id, drained=True)
    counters.newest = newest
    return counters


async def _handle(
    activity: Activity,
    *,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    models: Mapping[str, str],
    counters: DenialCounters,
) -> None:
    request_id = activity.request_id
    if not request_id:
        return
    counters.handled += 1
    address = deny_address(request_id)
    state = await store.seen(address)
    if state is Seen.TOMBSTONED:
        counters.tombstoned += 1
        return
    if state is Seen.LIVE:
        # The record already holds the content. This stamps what the feed
        # alone attests — that the block actually happened — and the agent
        # no frame carries. `contributed` is what turns the pushed event
        # into `anthropic-joined`; without it `to_event` would still call
        # a two-source record hook-sourced.
        #
        # Clearing DENIAL_ACTIVITY is what makes the record ready. Chunk 6
        # seeds that expectation on a denial whenever compliance is on,
        # precisely so the denial waits for this call instead of being
        # pushed and tombstoned seconds after the delivery. Forget it and
        # the record sits until the deadline with nothing left to wait for.
        outcome = await store.complete(
            address,
            {
                "event": {
                    "stop_reason": "guardrail_intervened",
                    "user_agent": activity.actor.user_agent,
                },
                "contributed": Append((COMPLIANCE,)),
            },
            (DENIAL_ACTIVITY,),
        )
        counters.completed += 1
        await push_if_ready(address, outcome, store=store, config=config, client=http)
        return
    user_id = activity.actor.user_id
    if not user_id:
        # The server rejects an Anthropic identity with no identifier.
        counters.dropped_no_identity += 1
        return
    event = _standalone(activity, request_id=request_id, user_id=user_id, models=models)
    # A real delivery id, so `webhook_ids` gets one: this is the same
    # `webhook-id` the hook would have filed, and a later frame revealing
    # the same delivery should land in the same list.
    outcome = await store.upsert(
        address,
        open_fields(event, webhook_id=request_id, contributed=COMPLIANCE),
        (),
    )
    counters.emitted += 1
    # No expectations, so the record is born ready: this claims, pushes and
    # retires, and the tombstone stops the next tick re-emitting it.
    await push_if_ready(address, outcome, store=store, config=config, client=http)


def _standalone(
    activity: Activity, *, request_id: str, user_id: str, models: Mapping[str, str]
) -> AIInvocationObservedV1:
    """The event when the hook never recorded this delivery.

    No surface carries the content of a denied call once the frame is
    gone, so the activity is the whole record: who, which conversation,
    which client, and that it was blocked.
    """
    conversation_id = activity.conversation_id
    model = models.get(conversation_id or "", "unknown")
    return AIInvocationObservedV1(
        request_id=request_id,
        timestamp=activity.created_at or "",
        identity_details=AnthropicIdentityDetails(user_id=user_id),
        model=AIModel(
            id=model,
            provider="anthropic",
            raw_model_id=None if model == "unknown" else model,
        ),
        # Overridden by `to_event` from `contributed`; set so the model
        # validates here, where a mistake is cheap to see.
        parsed_as=PARSED_AS_COMPLIANCE,
        stop_reason="guardrail_intervened",
        user_agent=activity.actor.user_agent or activity.surface,
        conversation_id=conversation_id,
    )
