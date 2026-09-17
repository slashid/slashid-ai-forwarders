"""Tests for the Cloud Audit Log reader.

Mocks ``google.cloud.logging.Client.list_entries()`` so tests don't
need real GCP credentials or network. AuditEntry parsing is exercised
against the actual LogEntry proto shapes we've seen in POC data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

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
    status: dict | None = None,
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
    if status is not None:
        payload["status"] = status

    entry = MagicMock()
    entry.insert_id = ""
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
    entry.insert_id = ""
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


def test_audit_entry_extracts_insert_id() -> None:
    """AuditEntry.from_log_entry pulls insertId from the LogEntry."""
    from slashid_vertex_forwarder.audit_source import AuditEntry

    entry = MagicMock()
    entry.insert_id = "hasty-piglet-42"
    entry.timestamp = datetime(2026, 9, 9, tzinfo=UTC)
    entry.payload = {
        "resourceName": "projects/p/locations/r/publishers/anthropic/models/claude-sonnet-4-5",
        "methodName": "google.cloud.aiplatform.v1.PredictionService.RawPredict",
    }

    a = AuditEntry.from_log_entry(entry)
    assert a.insert_id == "hasty-piglet-42"


def test_credential_chain_single_credential_from_direct_user() -> None:
    """Direct user (no impersonation) → single credential in chain.

    Co-located here in ``test_audit_source`` after ``_credential_chain``
    moved out of ``event_source.py``; the exhaustive
    length-2/length-3/oauth-handling coverage lives in
    ``test_bq_event_source`` alongside ``_consensus_chain``.
    """
    from slashid_vertex_forwarder.audit_source import AuditEntry, _credential_chain

    audit = AuditEntry.model_validate(
        {
            "timestamp": datetime(2026, 9, 9, tzinfo=UTC),
            "payload": {
                "authenticationInfo": {
                    "principalEmail": "user@example.com",
                    "principalSubject": "user:user@example.com",
                    "oauthInfo": {"oauthClientId": "764086051850-abc.apps.googleusercontent.com"},
                },
            },
        }
    )
    chain = _credential_chain(audit)
    assert len(chain) == 1
    assert chain[0].principal_email == "user@example.com"
    assert chain[0].principal_subject == "user:user@example.com"
    assert chain[0].oauth_client_id == "764086051850-abc.apps.googleusercontent.com"


def test_audit_entry_is_error_defaults_false_when_status_absent() -> None:
    """Successful audit entries have ``status: {}`` (or omit ``status``
    entirely). ``is_error`` must be False in both cases.
    """
    ts = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    a = AuditEntry.from_log_entry(_audit_log_entry(timestamp=ts, status=None))
    assert a.status_code == 0
    assert a.is_error is False

    a2 = AuditEntry.from_log_entry(_audit_log_entry(timestamp=ts, status={}))
    assert a2.status_code == 0
    assert a2.is_error is False


def test_audit_entry_is_error_true_when_status_code_nonzero() -> None:
    """Errored audit entry from strong-hue-507702-k7 smoke: OpenAI
    ``gpt-oss-120b-maas`` called via ``:rawPredict`` returned gRPC
    status code 9 (FAILED_PRECONDITION) with message "OpenMaaS model
    is not allowed to be called from this method."
    """
    ts = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    a = AuditEntry.from_log_entry(
        _audit_log_entry(
            timestamp=ts,
            status={"code": 9, "message": "OpenMaaS model is not allowed…"},
        )
    )
    assert a.status_code == 9
    assert a.is_error is True


def test_audit_entry_extracts_caller_supplied_user_agent() -> None:
    """``requestMetadata.callerSuppliedUserAgent`` is a standard
    ``google.cloud.audit.AuditLog`` field, present on every Vertex audit
    entry regardless of method. The GFE marker is stripped at ingest."""
    a = AuditEntry.model_validate(
        {
            "insertId": "ua-1",
            "timestamp": datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC),
            "payload": {
                "resourceName": "projects/p/locations/r/publishers/google/models/gemini-2.5-flash",
                "methodName": "google.cloud.aiplatform.v1.PredictionService.GenerateContent",
                "requestMetadata": {
                    "callerIp": "203.0.113.7",
                    "callerSuppliedUserAgent": "curl/8.5.0,gzip(gfe)",
                },
            },
        }
    )
    assert a.user_agent == "curl/8.5.0"


def test_audit_entry_user_agent_absent_is_none() -> None:
    """No ``requestMetadata`` block → ``user_agent`` stays None rather
    than raising."""
    a = AuditEntry.model_validate(
        {
            "insertId": "ua-2",
            "timestamp": datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC),
            "payload": {
                "resourceName": "projects/p/locations/r/publishers/google/models/gemini-2.5-flash",
                "methodName": "google.cloud.aiplatform.v1.PredictionService.GenerateContent",
            },
        }
    )
    assert a.user_agent is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Single GFE hop — the common case (376/391 observed entries).
        ("curl/8.5.0,gzip(gfe)", "curl/8.5.0"),
        # Two hops — what console/Studio traffic shows (15/391).
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36,gzip(gfe),gzip(gfe)",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36",
        ),
        # Hypothetical future variant — the token is matched loosely.
        ("some-client/2.0,br(gfe)", "some-client/2.0"),
        ("some-client/2.0,zstd(gfe),gzip(gfe)", "some-client/2.0"),
        # No suffix at all — untouched.
        ("plain-client/1.0", "plain-client/1.0"),
        # Parenthesised client UA must survive; only the (gfe) tail goes.
        ("Python-urllib/3.12,gzip(gfe)", "Python-urllib/3.12"),
        # A request sending NO User-Agent logs a bare marker with no
        # leading comma — observed live. Must still strip to None, or
        # we publish GFE noise as the client's identity.
        ("gzip(gfe)", None),
        # Same, but with the separator present.
        (",gzip(gfe)", None),
        # Bare marker, repeated.
        ("gzip(gfe),gzip(gfe)", None),
        (None, None),
    ],
)
def test_audit_entry_strips_gfe_user_agent_suffix(raw: str | None, expected: str | None) -> None:
    """Google Front End appends a constant marker per hop. Verified
    empirically that the token does not track Accept-Encoding —
    ``identity`` and an absent header both still yield ``gzip(gfe)`` —
    so it carries no information and is stripped at ingest."""
    payload: dict = {
        "resourceName": "projects/p/locations/r/publishers/google/models/gemini-2.5-flash",
        "methodName": "google.cloud.aiplatform.v1.PredictionService.GenerateContent",
    }
    if raw is not None:
        payload["requestMetadata"] = {"callerSuppliedUserAgent": raw}
    a = AuditEntry.model_validate(
        {"insertId": "ua", "timestamp": datetime(2026, 9, 17, tzinfo=UTC), "payload": payload}
    )
    assert a.user_agent == expected
