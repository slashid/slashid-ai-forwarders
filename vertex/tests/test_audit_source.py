"""Tests for the Cloud Audit Log reader.

Mocks ``google.cloud.logging.Client.list_entries()`` so tests don't
need real GCP credentials or network. AuditEntry parsing is exercised
against the actual LogEntry proto shapes we've seen in POC data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from slashid_vertex_forwarder.audit_source import (
    AuditEntry,
    query_audit_entries,
)


def _audit_log_entry(
    *,
    timestamp: datetime,
    resource_name: str = (
        "projects/p/locations/us-central1/publishers/google/models/gemini-2.5-flash"
    ),
    method_name: str = "google.cloud.aiplatform.v1.PredictionService.GenerateContent",
    principal_email: str | None = "alice@example.com",
    principal_subject: str | None = "user:alice@example.com",
    oauth_client_id: str | None = "32555940559.apps.googleusercontent.com",
    delegation: list[dict] | None = None,
):
    payload: dict = {
        "resourceName": resource_name,
        "methodName": method_name,
        "authenticationInfo": {},
    }
    ai = payload["authenticationInfo"]
    if principal_email is not None:
        ai["principalEmail"] = principal_email
    if principal_subject is not None:
        ai["principalSubject"] = principal_subject
    if oauth_client_id is not None:
        ai["oauthInfo"] = {"oauthClientId": oauth_client_id}
    if delegation is not None:
        ai["serviceAccountDelegationInfo"] = delegation

    entry = MagicMock()
    entry.timestamp = timestamp
    entry.payload = payload
    return entry


def test_audit_entry_from_direct_user_call() -> None:
    ts = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    a = AuditEntry.from_log_entry(_audit_log_entry(timestamp=ts))
    assert a.timestamp == ts
    assert a.resource_name.endswith("/publishers/google/models/gemini-2.5-flash")
    assert a.method_name.endswith(".GenerateContent")
    assert a.effective_principal_email == "alice@example.com"
    assert a.effective_principal_subject == "user:alice@example.com"
    assert a.effective_oauth_client_id == "32555940559.apps.googleusercontent.com"
    assert a.delegation_chain == []


def test_audit_entry_from_service_account_call_no_oauth() -> None:
    a = AuditEntry.from_log_entry(
        _audit_log_entry(
            timestamp=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
            principal_email="sa@proj.iam.gserviceaccount.com",
            principal_subject="serviceAccount:sa@proj.iam.gserviceaccount.com",
            oauth_client_id=None,
        )
    )
    assert a.effective_oauth_client_id is None


def test_audit_entry_from_impersonation_1_hop() -> None:
    delegation = [
        {
            "principalSubject": "user:alice@example.com",
            "firstPartyPrincipal": {"principalEmail": "alice@example.com"},
        }
    ]
    a = AuditEntry.from_log_entry(
        _audit_log_entry(
            timestamp=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
            principal_email="sa@proj.iam.gserviceaccount.com",
            principal_subject="serviceAccount:sa@proj.iam.gserviceaccount.com",
            oauth_client_id=None,
            delegation=delegation,
        )
    )
    assert len(a.delegation_chain) == 1
    assert a.delegation_chain[0].principal_subject == "user:alice@example.com"
    assert a.delegation_chain[0].first_party_email == "alice@example.com"
    assert a.effective_principal_email == "sa@proj.iam.gserviceaccount.com"


def test_audit_entry_from_impersonation_2_hops() -> None:
    delegation = [
        {
            "principalSubject": "user:alice@example.com",
            "firstPartyPrincipal": {"principalEmail": "alice@example.com"},
        },
        {
            "principalSubject": "serviceAccount:sa1@proj.iam.gserviceaccount.com",
            "firstPartyPrincipal": {"principalEmail": "sa1@proj.iam.gserviceaccount.com"},
        },
    ]
    a = AuditEntry.from_log_entry(
        _audit_log_entry(
            timestamp=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
            principal_email="sa2@proj.iam.gserviceaccount.com",
            principal_subject="serviceAccount:sa2@proj.iam.gserviceaccount.com",
            oauth_client_id=None,
            delegation=delegation,
        )
    )
    assert len(a.delegation_chain) == 2
    assert a.delegation_chain[0].first_party_email == "alice@example.com"
    assert a.delegation_chain[1].first_party_email == "sa1@proj.iam.gserviceaccount.com"


def test_audit_entry_missing_authentication_info_stubs_out() -> None:
    entry = MagicMock()
    entry.timestamp = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    entry.payload = {
        "resourceName": "projects/p/locations/us-central1/publishers/google/models/x",
        "methodName": "google.cloud.aiplatform.v1.PredictionService.GenerateContent",
    }
    a = AuditEntry.from_log_entry(entry)
    assert a.effective_principal_email is None
    assert a.effective_principal_subject is None
    assert a.effective_oauth_client_id is None
    assert a.delegation_chain == []


def test_query_audit_entries_builds_filter_and_calls_client() -> None:
    """Verify the filter string + ordering + time window passed to list_entries."""
    client = MagicMock()
    client.list_entries.return_value = iter([])  # empty iterator

    ts_lo = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    ts_hi = datetime(2026, 9, 9, 12, 0, 2, tzinfo=UTC)
    entries = query_audit_entries(
        client=client, project_id="p", region="us-central1", ts_range=(ts_lo, ts_hi)
    )
    assert entries == []
    client.list_entries.assert_called_once()
    kwargs = client.list_entries.call_args.kwargs
    filter_str = kwargs["filter_"]
    assert 'timestamp>="2026-09-09T12:00:00' in filter_str
    assert 'timestamp<="2026-09-09T12:00:02' in filter_str
    assert 'protoPayload.methodName:"generateContent"' in filter_str
    assert 'protoPayload.serviceName="aiplatform.googleapis.com"' in filter_str
    assert 'protoPayload.resourceName:"/locations/us-central1/"' in filter_str
    assert kwargs["order_by"] == "timestamp asc"
    assert kwargs["resource_names"] == ["projects/p"]
