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

from slashid_vertex_forwarder.config import Config
from slashid_vertex_forwarder.event_source import Checkpoint


def _config() -> Config:
    return Config(
        endpoint="https://api.slashid.com",
        push_token="t" * 32,
        gcp_project_id="vertex-test-507702",
        gcp_region="us-central1",
    )


class _FakeLoggingClient:
    """Captures the filter + order_by from list_entries(); returns
    a prescribed list of fake LogEntry objects."""

    def __init__(self, entries: list[Any] | None = None) -> None:
        self._entries = list(entries) if entries else []
        self.calls: list[dict[str, Any]] = []

    def list_entries(self, **kwargs: Any) -> list[Any]:
        self.calls.append(dict(kwargs))
        return self._entries


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


def _fake_log_entry(
    *,
    insert_id: str,
    timestamp: datetime,
    resource_name: str,
    method_name: str = "google.cloud.aiplatform.v1.PredictionService.RawPredict",
    principal_email: str = "user@example.com",
) -> Any:
    class _FakeLogEntry:
        pass

    entry = _FakeLogEntry()
    entry.insert_id = insert_id
    entry.timestamp = timestamp
    entry.payload = {
        "resourceName": resource_name,
        "methodName": method_name,
        "authenticationInfo": {
            "principalEmail": principal_email,
            "principalSubject": f"user:{principal_email}",
        },
    }
    return entry


class _FakeCheckpointStore:
    def __init__(self, initial: Checkpoint | None = None) -> None:
        self._value = initial if initial is not None else Checkpoint(None, None)
        self.saves: list[Checkpoint] = []

    def load(self) -> Checkpoint:
        return self._value

    def save(self, checkpoint: Checkpoint) -> None:
        self._value = checkpoint
        self.saves.append(checkpoint)


def test_fetch_yields_events_and_advances_next_checkpoint() -> None:
    from slashid_vertex_forwarder.audit_only_source import AuditOnlyEventSource

    t1 = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 9, 12, 0, 1, tzinfo=UTC)
    fake_client = _FakeLoggingClient(
        [
            _fake_log_entry(
                insert_id="a",
                timestamp=t1,
                resource_name="projects/p/locations/r/publishers/anthropic/models/claude-sonnet-4-5",
            ),
            _fake_log_entry(
                insert_id="b",
                timestamp=t2,
                resource_name="projects/p/locations/r/publishers/anthropic/models/claude-sonnet-4-5",
            ),
        ]
    )
    source = AuditOnlyEventSource(
        logging_client=fake_client,
        checkpoint_store=_FakeCheckpointStore(),
        project_id="p",
        region="r",
        observed_models=["anthropic/claude-sonnet-4-5"],
        max_entries_per_tick=100,
        config=_config(),
    )
    events, next_cp = source.fetch()
    assert len(events) == 2
    # Verify shape of the AIInvocationObservedV1 events.
    assert events[0].parsed_as == "vertex-audit"
    assert events[0].model.provider == "anthropic"
    assert events[0].model.name == "claude-sonnet-4-5"
    assert events[0].model.id == "publishers/anthropic/models/claude-sonnet-4-5"
    assert events[0].tokens.input == 0
    # Sparse-by-design: audit logs carry no request/response payload,
    # so every derived field stays null. ``stop_reason`` and ``output``
    # are nulled post-build (the shared builder leaves the
    # ``stop_reason="unknown"`` sentinel in place; AuditOnlyEventSource
    # strips it for the audit path).
    assert events[0].input is None
    assert events[0].output is None
    assert events[0].stop_reason is None
    assert events[0].used_tools is None
    assert events[0].accessed_files is None
    assert events[0].available_tools is None
    # Identity is populated by _credential_chain(audit).
    assert events[0].identity_details.credential_chain is not None
    assert len(events[0].identity_details.credential_chain) >= 1
    assert next_cp == Checkpoint(timestamp=t2, id="b")


def test_fetch_advances_next_checkpoint_across_filter_drops() -> None:
    """Entry has publisher-in-list but model-not-in-list: event
    dropped, next_checkpoint still advances past the raw entry."""
    from slashid_vertex_forwarder.audit_only_source import AuditOnlyEventSource

    t1 = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    fake_client = _FakeLoggingClient(
        [
            _fake_log_entry(
                insert_id="a",
                timestamp=t1,
                resource_name="projects/p/locations/r/publishers/anthropic/models/claude-haiku-3-5",
            ),
        ]
    )
    source = AuditOnlyEventSource(
        logging_client=fake_client,
        checkpoint_store=_FakeCheckpointStore(),
        project_id="p",
        region="r",
        observed_models=["anthropic/claude-sonnet-4-5"],
        max_entries_per_tick=100,
        config=_config(),
    )
    events, next_cp = source.fetch()
    assert events == []
    assert next_cp == Checkpoint(timestamp=t1, id="a")


def test_fetch_advances_next_checkpoint_across_parse_failures() -> None:
    """Entry with malformed resource_name is dropped; checkpoint
    still advances."""
    from slashid_vertex_forwarder.audit_only_source import AuditOnlyEventSource

    t1 = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    fake_client = _FakeLoggingClient(
        [
            _fake_log_entry(insert_id="a", timestamp=t1, resource_name="not-a-path"),
        ]
    )
    source = AuditOnlyEventSource(
        logging_client=fake_client,
        checkpoint_store=_FakeCheckpointStore(),
        project_id="p",
        region="r",
        observed_models=["anthropic/claude-sonnet-4-5"],
        max_entries_per_tick=100,
        config=_config(),
    )
    events, next_cp = source.fetch()
    assert events == []
    assert next_cp == Checkpoint(timestamp=t1, id="a")


def test_fetch_empty_result_returns_none_next_checkpoint() -> None:
    from slashid_vertex_forwarder.audit_only_source import AuditOnlyEventSource

    source = AuditOnlyEventSource(
        logging_client=_FakeLoggingClient([]),
        checkpoint_store=_FakeCheckpointStore(),
        project_id="p",
        region="r",
        observed_models=["anthropic/claude-sonnet-4-5"],
        max_entries_per_tick=100,
        config=_config(),
    )
    events, next_cp = source.fetch()
    assert events == []
    assert next_cp is None


def test_commit_saves_to_checkpoint_store() -> None:
    from slashid_vertex_forwarder.audit_only_source import AuditOnlyEventSource

    store = _FakeCheckpointStore()
    source = AuditOnlyEventSource(
        logging_client=_FakeLoggingClient([]),
        checkpoint_store=store,
        project_id="p",
        region="r",
        observed_models=[],
        max_entries_per_tick=100,
        config=_config(),
    )
    cp = Checkpoint(timestamp=datetime(2026, 9, 9, tzinfo=UTC), id="x")
    source.commit(cp)
    assert store.saves == [cp]
