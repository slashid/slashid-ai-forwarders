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

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from slashid_ai_forwarder_core.events import GCPIdentityDetails

from .audit_source import AuditEntry
from .event_source import Checkpoint

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
