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
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    GCPIdentityDetails,
)

from .audit_source import AuditEntry
from .event_source import Checkpoint

if TYPE_CHECKING:
    from .checkpoint_store import CheckpointStore
    from .config import Config

log = logging.getLogger(__name__)


@dataclass
class AuditOnlyEntry:
    """One audit-log invocation projected to the wire-relevant fields.

    ``model_path`` is the canonical short form (matches what BQ payload
    entries carry). ``publisher`` and ``model`` are pre-parsed for the
    envelope builder. ``method_name`` is the raw FQN (e.g.
    ``google.cloud.aiplatform.v1.PredictionService.RawPredict``);
    ``_short_method`` peels the tail suffix for the wire event.
    """

    insert_id: str
    timestamp: datetime
    resource_name: str
    method_name: str
    model_path: str
    publisher: str
    model: str
    region: str
    identity_details: GCPIdentityDetails

    @property
    def checkpoint(self) -> Checkpoint:
        return Checkpoint(timestamp=self.timestamp, id=self.insert_id)


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
        parts.append(
            f"(timestamp>{cp_ts} "
            f"OR (timestamp={cp_ts} AND insertId>{cp_id}))"
        )
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

    The full source-specific pipeline lives inside ``fetch``:
    Cloud Logging query → parse → filter → envelope → build final
    ``AIInvocationObservedV1`` via ``build_event_from_normalized`` on
    an empty ``NormalizedInvocation()``. Audit-only events are sparse
    by design — no request/response payload exists to normalize.
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

        entries: list[AuditOnlyEntry] = []
        for audit in raw_sorted:
            entry = self._to_audit_only_entry(audit)
            if entry is not None:
                entries.append(entry)

        if not entries:
            return [], next_cp

        events = asyncio.run(self._build_events(entries))
        return events, next_cp

    def commit(self, checkpoint: Checkpoint) -> None:
        """Advance the source's checkpoint. Called by the handler after
        successful wire push."""
        self._checkpoint_store.save(checkpoint)

    def _to_audit_only_entry(self, audit: AuditEntry) -> AuditOnlyEntry | None:
        """Parse an AuditEntry into an AuditOnlyEntry. Drops the entry
        when the resource_name doesn't match ``publishers/<pub>/models/<model>``
        or when ``<pub>/<model>`` is not in ``observed_models``."""
        from .audit_source import _credential_chain
        from .event_envelope import _parse_model_path

        publisher, model = _parse_model_path(audit.resource_name)
        if publisher is None or model is None:
            log.warning(
                "dropping audit entry with unparseable resource_name: %s (insertId=%s)",
                audit.resource_name,
                audit.insert_id,
            )
            return None
        pair = f"{publisher}/{model}"
        if pair not in self._observed_models:
            return None

        chain = _credential_chain(audit)
        return AuditOnlyEntry(
            insert_id=audit.insert_id,
            timestamp=audit.timestamp,
            resource_name=audit.resource_name,
            method_name=audit.method_name,
            model_path=f"publishers/{publisher}/models/{model}",
            publisher=publisher,
            model=model,
            region=self._region,
            identity_details=GCPIdentityDetails(
                credential_chain=chain or None,
            ),
        )

    async def _build_events(
        self, entries: list[AuditOnlyEntry]
    ) -> list[AIInvocationObservedV1]:
        """Turn AuditOnlyEntry list into final wire events.

        Audit-only entries carry no request/response payload, so the
        canonical ``NormalizedInvocation`` is intentionally empty; the
        shared builder leaves ``input`` / ``output`` / ``used_tools`` /
        ``accessed_files`` at their null defaults.
        """
        from slashid_ai_forwarder_core.events import build_event_from_normalized
        from slashid_ai_forwarder_core.normalize.normalized.types import (
            NormalizedInvocation,
        )

        from .event_envelope import vertex_audit_only_envelope

        async def _build(entry: AuditOnlyEntry) -> AIInvocationObservedV1 | None:
            envelope = vertex_audit_only_envelope(entry)
            if envelope is None:
                return None
            event = await build_event_from_normalized(
                NormalizedInvocation(), envelope, config=self._config
            )
            # NormalizedInvocationOutput.stop_reason defaults to the
            # ``"unknown"`` sentinel, which the shared builder passes
            # through to the wire. Audit-only events know nothing about
            # stop_reason — null it out post-build so the wire event
            # matches the sparse-fields contract in the design doc.
            # Also drop ``output`` (which the shared builder populated
            # with a hash of ``{stop_reason: "unknown"}``).
            event.stop_reason = None
            event.output = None
            return event

        built_or_none = await asyncio.gather(*(_build(e) for e in entries))
        return [e for e in built_or_none if e is not None]
