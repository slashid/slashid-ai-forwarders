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

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from slashid_ai_forwarder_core.events import GCPIdentityDetails

from .event_source import Checkpoint

if TYPE_CHECKING:
    pass

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
