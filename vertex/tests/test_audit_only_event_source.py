"""Tests for AuditOnlyEventSource — audit-log-only observability
for non-Google publishers on Vertex Model Garden.

Uses a fake google.cloud.logging.Client that returns pre-built
LogEntry shapes. AuditEntry parsing is already covered by
test_audit_source; here we exercise the source's fetch/commit
lifecycle, filter construction, and envelope emission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from slashid_ai_forwarder_core.events import GCPIdentityDetails

from slashid_vertex_forwarder.event_source import Checkpoint


class _FakeLoggingClient:
    """Captures the filter + order_by from list_entries(); returns
    a prescribed list of fake LogEntry objects."""

    def __init__(self, entries: list[Any] | None = None) -> None:
        self._entries = list(entries) if entries else []
        self.calls: list[dict[str, Any]] = []

    def list_entries(self, **kwargs: Any) -> list[Any]:
        self.calls.append(dict(kwargs))
        return self._entries


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


def test_query_audit_only_entries_filter_includes_compound_tiebreak() -> None:
    """Filter must include the compound (timestamp, id) > checkpoint
    tuple — verified empirically to work server-side on Cloud Logging."""
    from slashid_vertex_forwarder.audit_only_source import query_audit_only_entries

    client = _FakeLoggingClient()
    cp = Checkpoint(
        timestamp=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
        id="audit-xyz",
    )
    query_audit_only_entries(
        client=client,
        project_id="p1",
        region="europe-west1",
        checkpoint=cp,
        max_entries=1000,
    )
    filter_ = client.calls[0]["filter_"]
    assert 'resource.type="audited_resource"' in filter_
    assert 'protoPayload.serviceName="aiplatform.googleapis.com"' in filter_
    assert 'NOT protoPayload.resourceName:"/publishers/google/"' in filter_
    assert "/locations/europe-west1/" in filter_
    # Compound tie-break:
    assert 'timestamp>"2026-09-09T12:00:00+00:00"' in filter_
    assert 'timestamp="2026-09-09T12:00:00+00:00"' in filter_
    assert 'insertId>"audit-xyz"' in filter_
    assert client.calls[0]["order_by"] == "timestamp asc"
    assert client.calls[0]["resource_names"] == ["projects/p1"]
    assert client.calls[0]["max_results"] == 1000


def test_query_audit_only_entries_filter_empty_checkpoint_omits_tiebreak() -> None:
    """First-tick case: cp.timestamp is None. Filter omits the
    checkpoint tie-break clause."""
    from slashid_vertex_forwarder.audit_only_source import query_audit_only_entries

    client = _FakeLoggingClient()
    query_audit_only_entries(
        client=client,
        project_id="p1",
        region="europe-west1",
        checkpoint=Checkpoint(timestamp=None, id=None),
        max_entries=1000,
    )
    filter_ = client.calls[0]["filter_"]
    assert "insertId>" not in filter_
    assert "timestamp>" not in filter_
