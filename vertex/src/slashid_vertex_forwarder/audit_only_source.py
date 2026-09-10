"""Audit-log-only event source — Vertex Model Garden non-Google publishers.

Complementary to ``BqEventSource``. Whereas BQ payload logging is
Google-only (setPublisherModelConfig silently no-ops on non-Google
publishers), Cloud Audit Logs record every ``rawPredict`` /
``streamRawPredict`` / ``predict`` invocation on every publisher.
This source polls those entries and emits sparse
``AIInvocationObservedV1`` events: identity + call shape only, no
payload.

Server-side filter is publisher-level (non-Google in the configured
region for the relevant methods); client-side, entries are further
narrowed to the customer's ``observed_models`` allowlist. The
compound checkpoint tie-break ``(timestamp, id) > (cp.timestamp, cp.id)``
is server-side — Cloud Logging honors lexicographic string comparison
on ``insertId`` (verified empirically 2026-09-09).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    EventEnvelope,
)

from .audit_source import AuditEntry
from .event_source import Checkpoint

if TYPE_CHECKING:
    from .checkpoint_store import CheckpointStore
    from .config import Config

log = logging.getLogger(__name__)


def query_audit_only_entries(
    *,
    client: Any,  # google.cloud.logging.Client (REST transport)
    project_id: str,
    region: str,
    checkpoint: Checkpoint,
    max_entries: int,
) -> list[AuditEntry]:
    """Fetch non-Google-publisher audit entries past the checkpoint.

    The compound ``(timestamp, id) > checkpoint`` filter is server-side.
    Cloud Logging's ``order_by="timestamp asc"`` orders by timestamp
    only — callers sort by ``(timestamp, insert_id)`` for the compound
    order the checkpoint contract expects.

    Every interpolated string uses ``json.dumps`` for filter-language
    escaping (same pattern Phase 3.6 uses).
    """
    method_clause = (
        'protoPayload.methodName:"rawPredict" '
        'OR protoPayload.methodName:"predict" '
        'OR protoPayload.methodName:"streamGenerateContent" '
        'OR protoPayload.methodName:"generateContent"'
    )
    parts = [
        'resource.type="audited_resource"',
        'protoPayload.serviceName="aiplatform.googleapis.com"',
        'protoPayload.resourceName:"/publishers/"',
        'NOT protoPayload.resourceName:"/publishers/google/"',
        f"protoPayload.resourceName:{json.dumps(f'/locations/{region}/')}",
        f"resource.labels.project_id={json.dumps(project_id)}",
        f"({method_clause})",
    ]
    if checkpoint.timestamp is not None and checkpoint.id is not None:
        cp_ts = json.dumps(checkpoint.timestamp.isoformat())
        cp_id = json.dumps(checkpoint.id)
        parts.append(f"(timestamp>{cp_ts} OR (timestamp={cp_ts} AND insertId>{cp_id}))")
    filter_ = " AND ".join(parts)
    return [
        AuditEntry.from_log_entry(e)
        for e in client.list_entries(
            resource_names=[f"projects/{project_id}"],
            filter_=filter_,
            order_by="timestamp asc",
            max_results=max_entries,
        )
    ]


class AuditOnlyEventSource:
    """Poll Cloud Audit Logs for non-Google publisher invocations.

    Constructor takes ``observed_models`` — the list of ``<pub>/<model>``
    slugs the customer wants observed. Server-side filter is
    publisher-level (non-Google); client-side each entry's
    ``<pub>/<model>`` is compared against the allowlist.

    ``fetch`` runs the full source-specific pipeline: Cloud Logging
    query → sort → per-entry parse + observed_models filter → envelope
    → build final ``AIInvocationObservedV1`` via
    ``build_event_from_normalized`` on an empty ``NormalizedInvocation()``.
    Audit-only events are sparse by design — no request/response payload
    exists to normalize.
    """

    def __init__(
        self,
        *,
        # google.cloud.logging.Client (REST transport) — kept untyped so
        # GCP client deps don't bleed into the type-check surface.
        logging_client: Any,
        checkpoint_store: CheckpointStore,
        project_id: str,
        region: str,
        observed_models: Sequence[str],
        max_entries_per_tick: int,
        config: Config,
    ) -> None:
        self._logging_client = logging_client
        self._checkpoint_store = checkpoint_store
        self._project_id = project_id
        self._region = region
        self._observed_models = set(observed_models)
        self._max_entries_per_tick = max_entries_per_tick
        self._config = config

    def fetch(self) -> tuple[list[AIInvocationObservedV1], Checkpoint | None]:
        """Query audit entries past this source's checkpoint, filter to
        the customer's ``observed_models`` allowlist, build final wire
        events.

        ``next_checkpoint`` reflects the max ``(timestamp, insert_id)``
        across ALL raw audit entries — including entries dropped by the
        parse or ``observed_models`` filter — so permanent misses never
        stall the pipeline. ``None`` on zero raw entries.
        """
        from .event_envelope import _parse_model_path, vertex_audit_only_envelope

        checkpoint = self._checkpoint_store.load()
        raw = query_audit_only_entries(
            client=self._logging_client,
            project_id=self._project_id,
            region=self._region,
            checkpoint=checkpoint,
            max_entries=self._max_entries_per_tick,
        )
        if not raw:
            return [], None

        # Sort by (timestamp, insert_id) — Cloud Logging orders by
        # timestamp only, so ties need Python resolution to match the
        # checkpoint contract.
        raw_sorted = sorted(raw, key=lambda a: (a.timestamp, a.insert_id))
        next_cp = Checkpoint(
            timestamp=raw_sorted[-1].timestamp,
            id=raw_sorted[-1].insert_id,
        )

        envelopes: list[EventEnvelope] = []
        for audit in raw_sorted:
            publisher, model = _parse_model_path(audit.resource_name)
            if publisher is None or model is None:
                log.warning(
                    "dropping audit entry with unparseable resource_name: %s (insertId=%s)",
                    audit.resource_name,
                    audit.insert_id,
                )
                continue
            if f"{publisher}/{model}" not in self._observed_models:
                continue
            envelope = vertex_audit_only_envelope(audit)
            if envelope is not None:
                envelopes.append(envelope)

        if not envelopes:
            return [], next_cp

        events = asyncio.run(self._build_events(envelopes))
        return events, next_cp

    def commit(self, checkpoint: Checkpoint) -> None:
        """Advance the source's checkpoint. Called by the handler after
        successful wire push."""
        self._checkpoint_store.save(checkpoint)

    async def _build_events(self, envelopes: list[EventEnvelope]) -> list[AIInvocationObservedV1]:
        """Turn a list of envelopes into final wire events.

        Audit-only events carry no invocation shape — the canonical
        ``NormalizedInvocation`` is intentionally empty. The shared
        builder leaves ``input`` / ``used_tools`` / ``accessed_files``
        null at their defaults; ``stop_reason`` (defaults to the
        ``"unknown"`` sentinel) and ``output`` (populated with a hash
        of that sentinel) are nulled post-build so the wire event
        matches the sparse-fields contract.
        """
        from slashid_ai_forwarder_core.events import build_event_from_normalized
        from slashid_ai_forwarder_core.normalize.normalized.types import (
            NormalizedInvocation,
        )

        async def _build(envelope: EventEnvelope) -> AIInvocationObservedV1:
            event = await build_event_from_normalized(
                NormalizedInvocation(), envelope, config=self._config
            )
            event.stop_reason = None
            event.output = None
            return event

        return list(await asyncio.gather(*(_build(e) for e in envelopes)))
