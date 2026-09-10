"""Tests for AuditOnlyEventSource — audit-log-only observability
for non-Google publishers on Vertex Model Garden.

Uses a fake google.cloud.logging.Client that returns pre-built
LogEntry shapes. AuditEntry parsing is already covered by
test_audit_source; here we exercise the source's fetch/commit
lifecycle, filter construction, and envelope emission.
"""

from __future__ import annotations

from datetime import UTC, datetime

from slashid_ai_forwarder_core.events import GCPIdentityDetails

from slashid_vertex_forwarder.event_source import Checkpoint


def test_audit_only_entry_checkpoint_property() -> None:
    """AuditOnlyEntry.checkpoint returns a Checkpoint at (timestamp, insert_id)."""
    from slashid_vertex_forwarder.audit_only_source import AuditOnlyEntry

    ts = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    entry = AuditOnlyEntry(
        insert_id="log-abc",
        timestamp=ts,
        resource_name="projects/p/locations/us-central1/publishers/anthropic/models/claude-sonnet-4-5",
        method_name="google.cloud.aiplatform.v1.PredictionService.RawPredict",
        model_path="publishers/anthropic/models/claude-sonnet-4-5",
        publisher="anthropic",
        model="claude-sonnet-4-5",
        region="us-central1",
        identity_details=GCPIdentityDetails(),
    )
    assert entry.checkpoint == Checkpoint(timestamp=ts, id="log-abc")
