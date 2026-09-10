"""Audit-log-only event source — Vertex Model Garden non-Google publishers.

Complementary to ``BqEventSource``. Whereas BQ payload logging is
Google-only (setPublisherModelConfig silently no-ops on non-Google
publishers), Cloud Audit Logs record every ``rawPredict`` /
``streamRawPredict`` / ``predict`` invocation on every publisher.
This source polls those entries and emits sparse
``AIInvocationObservedV1`` events: identity + call shape only, no
payload.

Server-side filter is publisher-level (non-Google in the configured
region for the relevant methods) plus a coarse ``timestamp >= cp_ts``.
Client-side, entries are further narrowed to the customer's
``observed_models`` allowlist and the strict compound
``(timestamp, id) > (cp.timestamp, cp.id)`` is re-applied in Python.
The compound must run client-side because Cloud Logging stores audit
timestamps at nanosecond precision (``…432004832Z``) while the Python
client library truncates them to microsecond (``…432004``) on parse;
sending a strict ``timestamp>`` with the truncated μs cp gets
re-matched by the server's ns comparator, and the tie-break on
``insertId`` never fires.
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
    """Fetch non-Google-publisher audit entries at or past the checkpoint.

    Server-side filter is loose (``timestamp >= cp_ts``). Cloud Logging
    audit entries have **nanosecond**-precision timestamps on the wire
    (``…432004832Z``) but the Python client's parser truncates them to
    **microsecond** (``…432004``). If we sent a strict ``timestamp>cp_ts``
    with the truncated cp back to the server, the server's ns-precision
    comparator would re-match the same entry — its true ns tail is
    strictly greater than the truncated μs watermark, so the tie-break
    on ``insertId`` never fires.

    The fix is to keep the server filter coarse and re-apply the
    strict compound ``(timestamp, insert_id) > (cp_ts, cp_id)`` in
    Python, where both sides operate at μs precision. The caller sorts
    and applies the exact filter after this returns.

    Over-fetch cost is bounded by the number of entries sharing the
    watermark's μs — one in practice.

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
    if checkpoint.timestamp is not None:
        cp_ts = json.dumps(checkpoint.timestamp.isoformat())
        parts.append(f"timestamp>={cp_ts}")
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
        # checkpoint contract. The server-side filter is coarse
        # (``timestamp >= cp_ts``) because sending a strict ``>`` with
        # a μs-truncated cp gets re-matched by the ns-precision server
        # (the client library truncated ``.432004832`` to ``.432004``,
        # so ``server_ns > client_μs`` is always true for the source
        # entry). We re-apply the strict compound ``(timestamp, id) >
        # (cp_ts, cp_id)`` here in Python, where both sides live at μs.
        raw_filtered = sorted(raw, key=lambda a: (a.timestamp, a.insert_id))
        if checkpoint.timestamp is not None and checkpoint.id is not None:
            cp_pair = (checkpoint.timestamp, checkpoint.id)
            raw_filtered = [a for a in raw_filtered if (a.timestamp, a.insert_id) > cp_pair]
        if not raw_filtered:
            # Server returned only entries we've already seen (typically
            # just the watermark itself echoing back through the coarse
            # ``>=`` filter). Nothing to emit, nothing to advance.
            return [], None
        next_cp = Checkpoint(
            timestamp=raw_filtered[-1].timestamp,
            id=raw_filtered[-1].insert_id,
        )

        envelopes: list[EventEnvelope] = []
        for audit in raw_filtered:
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
