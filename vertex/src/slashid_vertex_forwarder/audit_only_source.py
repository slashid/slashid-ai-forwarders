"""Audit-log-only event source — non-Google publishers + errored Google calls.

Complementary to ``BqEventSource``. Vertex's payload BQ logging
(``setPublisherModelConfig``) covers only successful Google-publisher
invocations — non-Google publishers get no BQ path at all, and
errored Google calls are dropped from BQ too (response-conditional).
Cloud Audit Logs record every ``rawPredict`` / ``streamRawPredict`` /
``predict`` invocation on every publisher regardless of status, so
this source picks up both gaps and emits sparse
``AIInvocationObservedV1`` events: identity + call shape only, no
payload.

Server-side filter is publisher-scoped
(``NOT publishers/google/ OR protoPayload.status.code!=0`` in the
configured region for the relevant methods) plus a coarse
``timestamp >= cp_ts``. The non-Google-OR-error clause is what
prevents double-counting: successful Google calls end up in BQ only;
errored Google calls end up here only; non-Google calls end up here
regardless of status.
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
from typing import TYPE_CHECKING

from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    EventEnvelope,
)
from slashid_ai_forwarder_core.platform import Checkpoint, CheckpointStore

from .audit_source import AuditEntry

if TYPE_CHECKING:
    from google.cloud.logging import Client as LoggingClient

    from .config import Config

log = logging.getLogger(__name__)


def query_audit_only_entries(
    *,
    client: LoggingClient,
    project_id: str,
    regions: Sequence[str],
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
    # Publisher scope:
    #  - Non-Google always captured — no BQ payload path for those.
    #  - Google captured only when the call errored — Vertex's payload
    #    BQ logging is response-conditional, so errored Google calls
    #    never land in BQ; the audit path is the only way to see them.
    #    Successful Google calls stay in BQ (captured by ``BqEventSource``)
    #    and MUST NOT be caught here or we double-count.
    # ``status.code!=0`` matches the same semantics as
    # ``AuditEntry.is_error`` (see audit_source.py) — the field is
    # absent on successful entries and Cloud Logging's ``!=`` on a
    # missing field evaluates false, so only errored entries match.
    publisher_scope = (
        'NOT protoPayload.resourceName:"/publishers/google/" OR protoPayload.status.code!=0'
    )
    # Multi-region: OR the per-region substring matches. Cloud Logging
    # aggregates audit entries globally, so a single query covers every
    # region the customer opted into via ``config.gcp_regions``.
    region_clause = " OR ".join(
        f"protoPayload.resourceName:{json.dumps(f'/locations/{r}/')}" for r in regions
    )
    parts = [
        'resource.type="audited_resource"',
        'protoPayload.serviceName="aiplatform.googleapis.com"',
        'protoPayload.resourceName:"/publishers/"',
        f"({publisher_scope})",
        f"({region_clause})",
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
        logging_client: LoggingClient,
        checkpoint_store: CheckpointStore,
        project_id: str,
        regions: Sequence[str],
        observed_models: Sequence[str],
        max_entries_per_tick: int,
        config: Config,
    ) -> None:
        self._logging_client = logging_client
        self._checkpoint_store = checkpoint_store
        self._project_id = project_id
        self._regions = list(regions)
        self._observed_models = set(observed_models)
        self._max_entries_per_tick = max_entries_per_tick
        self._config = config

    async def fetch(self) -> tuple[list[AIInvocationObservedV1], Checkpoint | None]:
        """Query audit entries past this source's checkpoint, filter to
        the customer's ``observed_models`` allowlist, build final wire
        events.

        ``next_checkpoint`` reflects the max ``(timestamp, insert_id)``
        across ALL raw audit entries — including entries dropped by the
        parse or ``observed_models`` filter — so permanent misses never
        stall the pipeline. ``None`` on zero raw entries.
        """
        from .event_envelope import _parse_model_path, vertex_audit_only_envelope

        checkpoint = await self._checkpoint_store.load()
        raw = await asyncio.to_thread(
            query_audit_only_entries,
            client=self._logging_client,
            project_id=self._project_id,
            regions=self._regions,
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
            publisher, model, _ = _parse_model_path(audit.resource_name)
            if publisher is None or model is None:
                log.warning(
                    "dropping audit entry with unparseable resource_name: %s (insertId=%s)",
                    audit.resource_name,
                    audit.insert_id,
                )
                continue
            # ``observed_models`` entries are always bare
            # (``<pub>/<model>``); Vertex may pin the audit entry with
            # an ``@YYYYMMDD`` version (Anthropic Claude on Vertex is
            # the canonical case), but the allowlist doesn't distinguish
            # versions — compare bare-to-bare.
            if f"{publisher}/{model}" not in self._observed_models:
                continue
            envelope = vertex_audit_only_envelope(audit)
            if envelope is not None:
                envelopes.append(envelope)

        if not envelopes:
            return [], next_cp

        return self._build_events(envelopes), next_cp

    async def commit(self, checkpoint: Checkpoint) -> None:
        """Advance the source's checkpoint. Called by the handler after
        successful wire push."""
        await self._checkpoint_store.save(checkpoint)

    def _build_events(self, envelopes: list[EventEnvelope]) -> list[AIInvocationObservedV1]:
        """Turn a list of envelopes into final wire events via
        ``build_sparse_event`` — envelope-only, no normalization pass,
        every conversation-shaped field stays ``None`` at the type
        level. ``is_error`` on the envelope maps to
        ``stop_reason="error"`` inside the shared builder.
        """
        from slashid_ai_forwarder_core.events import build_sparse_event

        return [build_sparse_event(e, config=self._config) for e in envelopes]
