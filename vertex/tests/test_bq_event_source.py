"""Tests for ``BqEventSource`` — polls a BigQuery wildcard-table union.

Uses a fake ``bigquery.Client`` doubles that captures the issued query
+ params and returns pre-built rows. The real client is heavyweight
(auth, discovery, retry) and covered by GCP integration tests outside
this suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from slashid_vertex_forwarder.event_source import BqEventSource, Checkpoint


@pytest.fixture(autouse=True)
def _stub_query_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent `fetch()` from hitting google.cloud.logging by default.

    Individual tests that want to assert on the audit-query wiring
    re-monkeypatch ``_query_audit`` explicitly."""
    monkeypatch.setattr(BqEventSource, "_query_audit", lambda self, ts_range: [])


class _FakeQueryJob:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def result(self) -> list[dict[str, Any]]:
        return self._rows


@dataclass
class _CapturedCall:
    query: str
    parameters: list[Any]


class _FakeBqClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[_CapturedCall] = []

    def query(self, query: str, job_config: Any) -> _FakeQueryJob:
        self.calls.append(_CapturedCall(query=query, parameters=list(job_config.query_parameters)))
        return _FakeQueryJob(self._rows)


_POC_MODEL_PATH = (
    "projects/vertex-test-507702/locations/us-central1/publishers/google/models/gemini-2.5-flash"
)
_POC_REQ = {
    "contents": [{"role": "user", "parts": [{"text": "Reply with only the word ACK-A"}]}],
    "model": _POC_MODEL_PATH,
}
_POC_RESP = {
    "candidates": [
        {"content": {"role": "model", "parts": [{"text": "ACK-A"}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 3, "totalTokenCount": 11},
}


def _row(
    *,
    request_id: int = 3292372995731278848,
    logging_time: datetime | None = None,
    model: str = "publishers/google/models/gemini-2.5-flash",
    api_method: str = "GenerateContent",
    metadata: dict[str, Any] | str | None = None,
    request_payload: dict[str, Any] | str | None = None,
    response_payload: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "logging_time": logging_time or datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC),
        "model": model,
        "api_method": api_method,
        "metadata": metadata,
        "full_request": request_payload or _POC_REQ,
        "full_response": response_payload or _POC_RESP,
    }


def _minimal_row(
    *,
    request_id: int = 1,
    logging_time: datetime | None = None,
    model: str = "publishers/google/models/gemini-2.5-flash",
    api_method: str = "GenerateContent",
    metadata: dict[str, Any] | None = None,
    full_request: dict[str, Any] | None = None,
    full_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Row-shaped dict for _row_to_entry — permissive, fills in required fields."""
    return {
        "request_id": request_id,
        "logging_time": logging_time or datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
        "model": model,
        "api_method": api_method,
        "metadata": json.dumps(metadata) if metadata is not None else None,
        "full_request": full_request or {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        "full_response": full_response or {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {},
        },
    }


def _source(*, rows: list[dict[str, Any]]) -> tuple[BqEventSource, _FakeBqClient]:
    client = _FakeBqClient(rows)
    src = BqEventSource(
        client=client,
        project_id="vertex-test-507702",
        dataset_id="slashid_vertex_reqresp_logs",
        region="us-central1",
        max_rows_per_tick=1000,
    )
    return src, client


def test_fetch_returns_validated_entries() -> None:
    src, _ = _source(rows=[_row()])
    entries = src.fetch(Checkpoint(None, None))
    assert len(entries) == 1
    e = entries[0]
    assert e.request_id == "3292372995731278848"
    assert e.model_path == "publishers/google/models/gemini-2.5-flash"
    assert e.region == "us-central1"
    # Pydantic validation ran — the parts are typed shapes now.
    assert e.request_body.contents[0].role == "user"
    assert e.response_body.candidates[0].finishReason == "STOP"


def test_fetch_reads_wildcard_table_pattern() -> None:
    """One wildcard FROM covers every provisioned per-model table."""
    src, client = _source(rows=[])
    src.fetch(Checkpoint(None, None))
    expected = "`vertex-test-507702.slashid_vertex_reqresp_logs.slashid_vertex_reqresp_*`"
    assert expected in client.calls[0].query


def test_fetch_no_checkpoint_omits_where_clause() -> None:
    src, client = _source(rows=[])
    src.fetch(Checkpoint(None, None))
    q = client.calls[0].query
    assert "WHERE" not in q
    # limit parameter always emitted.
    param_names = {p.name for p in client.calls[0].parameters}
    assert "limit" in param_names


def test_fetch_with_checkpoint_binds_where_params() -> None:
    src, client = _source(rows=[])
    src.fetch(Checkpoint(datetime(2026, 9, 5, tzinfo=UTC), "prev-id"))
    q = client.calls[0].query
    # Checkpoint clause is now paren-wrapped so it can AND with the
    # optional audit-buffer cutoff.
    assert "WHERE (logging_time > @last_time" in q
    param_names = {p.name for p in client.calls[0].parameters}
    assert param_names == {"limit", "last_time", "last_req"}


def test_fetch_ordering_clause() -> None:
    src, client = _source(rows=[])
    src.fetch(Checkpoint(None, None))
    assert "ORDER BY logging_time ASC, CAST(request_id AS STRING) ASC" in client.calls[0].query


def test_fetch_accepts_string_json_columns() -> None:
    """BQ JSON columns may surface as strings from some driver versions —
    handle both dict and string shapes."""
    row = _row(
        request_payload=json.dumps(_POC_REQ),
        response_payload=json.dumps(_POC_RESP),
    )
    src, _ = _source(rows=[row])
    entries = src.fetch(Checkpoint(None, None))
    assert len(entries) == 1


def test_fetch_drops_row_with_invalid_payload(caplog: pytest.LogCaptureFixture) -> None:
    row = _row(request_payload={"contents": "not-a-list-broken"})
    src, _ = _source(rows=[row])
    with caplog.at_level("WARNING", logger="slashid_vertex_forwarder.event_source"):
        entries = src.fetch(Checkpoint(None, None))
    assert entries == []
    assert any(
        "schema-invalid payload" in r.getMessage()
        and "request_id=3292372995731278848" in r.getMessage()
        for r in caplog.records
    )


def test_fetch_drops_row_with_missing_fields(caplog: pytest.LogCaptureFixture) -> None:
    row = _row()
    del row["request_id"]  # simulate a schema mismatch
    src, _ = _source(rows=[row])
    with caplog.at_level("WARNING", logger="slashid_vertex_forwarder.event_source"):
        entries = src.fetch(Checkpoint(None, None))
    assert entries == []
    assert any(
        "missing required fields" in r.getMessage() and "request_id" in r.getMessage()
        for r in caplog.records
    )


def test_fetch_drops_row_with_non_dict_payload(caplog: pytest.LogCaptureFixture) -> None:
    """Non-JSON strings still parse via json.loads but might come out as
    lists/numbers/strings — the shape check downstream drops them and
    logs the observed payload types."""
    row = _row(request_payload="42")  # json.loads → int
    src, _ = _source(rows=[row])
    with caplog.at_level("WARNING", logger="slashid_vertex_forwarder.event_source"):
        entries = src.fetch(Checkpoint(None, None))
    assert entries == []
    assert any(
        "non-dict payload" in r.getMessage() and "full_request=int" in r.getMessage()
        for r in caplog.records
    )


def test_fetch_multiple_rows_preserve_order() -> None:
    rows = [
        _row(request_id=1, logging_time=datetime(2026, 9, 5, 1, 0, 0, tzinfo=UTC)),
        _row(request_id=2, logging_time=datetime(2026, 9, 5, 1, 0, 1, tzinfo=UTC)),
        _row(request_id=3, logging_time=datetime(2026, 9, 5, 1, 0, 2, tzinfo=UTC)),
    ]
    src, _ = _source(rows=rows)
    entries = src.fetch(Checkpoint(None, None))
    assert [e.request_id for e in entries] == ["1", "2", "3"]


def test_entry_checkpoint_property_reflects_row() -> None:
    src, _ = _source(rows=[_row(request_id=42)])
    entry = src.fetch(Checkpoint(None, None))[0]
    cp = entry.checkpoint
    assert cp.last_request_id == "42"
    assert cp.last_logging_time == datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC)


def test_fetch_accepts_stream_generate_content_row() -> None:
    """BqEventSource treats StreamGenerateContent rows identically to
    GenerateContent — same code path, no api_method filtering. The
    row's ``api_method`` column is projected onto ``Entry.api_method``
    (used later by the identity-correlation join) but doesn't affect
    the fetch/normalize pipeline. Merged ``full_response`` has
    ``finishReason=null``; that's normalized downstream by
    ``resolve_finish_reason``, not here."""
    stream_req = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 50},
    }
    stream_resp = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "The"}]},
                "finishReason": None,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 3,
            "candidatesTokenCount": 50,
            "totalTokenCount": 53,
        },
    }
    src, _ = _source(rows=[_row(request_payload=stream_req, response_payload=stream_resp)])
    entries = src.fetch(Checkpoint(None, None))
    assert len(entries) == 1
    e = entries[0]
    assert e.request_body.generationConfig is not None
    assert e.request_body.generationConfig.maxOutputTokens == 50
    assert e.response_body.candidates[0].finishReason is None


def test_row_to_entry_extracts_api_method() -> None:
    """The api_method column projects to Entry.api_method."""
    from slashid_vertex_forwarder.event_source import _row_to_entry

    row = _minimal_row(
        api_method="StreamGenerateContent",
        # request/response payloads not relevant for this projection
    )
    entry = _row_to_entry(row, region="us-central1")
    assert entry is not None
    assert entry.api_method == "StreamGenerateContent"


def test_row_to_entry_extracts_request_latency_ms() -> None:
    """metadata.request_latency (float milliseconds) projects to
    Entry.request_latency_ms."""
    from slashid_vertex_forwarder.event_source import _row_to_entry

    row = _minimal_row(metadata={"request_latency": 1234.567})
    entry = _row_to_entry(row, region="us-central1")
    assert entry is not None
    assert entry.request_latency_ms == 1234.567


def test_row_to_entry_missing_request_latency_returns_none() -> None:
    """When metadata is absent or missing request_latency, latency_ms is None."""
    from slashid_vertex_forwarder.event_source import _row_to_entry

    row = _minimal_row(metadata=None)
    entry = _row_to_entry(row, region="us-central1")
    assert entry is not None
    assert entry.request_latency_ms is None

    row2 = _minimal_row(metadata={"other_field": 42})
    entry2 = _row_to_entry(row2, region="us-central1")
    assert entry2 is not None
    assert entry2.request_latency_ms is None


def test_entry_identity_details_defaults_to_empty() -> None:
    """Entry constructs with an empty GCPIdentityDetails; join stamps
    a populated one later."""
    from slashid_ai_forwarder_core.events import GCPIdentityDetails

    from slashid_vertex_forwarder.event_source import _row_to_entry

    row = _minimal_row()
    entry = _row_to_entry(row, region="us-central1")
    assert entry is not None
    assert isinstance(entry.identity_details, GCPIdentityDetails)
    assert entry.identity_details.credential_chain is None


def test_entry_identity_details_is_mutable() -> None:
    """Entry can be updated post-construction so the join can stamp identity."""
    from slashid_ai_forwarder_core.events import GCPCredential, GCPIdentityDetails

    from slashid_vertex_forwarder.event_source import _row_to_entry

    row = _minimal_row()
    entry = _row_to_entry(row, region="us-central1")
    assert entry is not None
    entry.identity_details = GCPIdentityDetails(
        credential_chain=[GCPCredential(principal_email="alice@example.com")]
    )
    assert entry.identity_details.credential_chain is not None
    assert entry.identity_details.credential_chain[0].principal_email == "alice@example.com"


def test_build_query_applies_buffer_cutoff() -> None:
    """When buffer_seconds > 0, the query includes a WHERE logging_time <= @cutoff clause."""
    source = BqEventSource(
        client=MagicMock(),
        project_id="p",
        dataset_id="d",
        region="us-central1",
        max_rows_per_tick=100,
        audit_buffer_seconds=30,
    )
    now = datetime(2026, 9, 9, 12, 0, 30, tzinfo=UTC)
    query, params = source._build_query(Checkpoint(None, None), now=now)
    assert "logging_time <= @cutoff" in query
    cutoff_param = next(p for p in params if p.name == "cutoff")
    assert cutoff_param.value == now - timedelta(seconds=30)


def test_build_query_projects_api_method_and_metadata() -> None:
    """SELECT list includes the new columns."""
    source = BqEventSource(
        client=MagicMock(),
        project_id="p",
        dataset_id="d",
        region="us-central1",
        max_rows_per_tick=100,
        audit_buffer_seconds=0,
    )
    query, _ = source._build_query(Checkpoint(None, None))
    assert "api_method" in query
    assert "metadata" in query


# --- Join helpers -----------------------------------------------------------


def _mk_entry(
    *,
    request_latency_ms: float | None = 200.0,
    logging_time: datetime | None = None,
    api_method: str = "GenerateContent",
    model_path: str = "publishers/google/models/gemini-2.5-flash",
    region: str = "us-central1",
):
    """Build a minimal Entry for join-helper tests."""
    from slashid_ai_forwarder_core.normalize.gemini.schema import (
        GeminiRequestBody,
        GeminiResponse,
    )

    from slashid_vertex_forwarder.event_source import Entry

    return Entry(
        request_id="1",
        logging_time=logging_time or datetime(2026, 9, 9, 12, 0, 0, 500000, tzinfo=UTC),
        model_path=model_path,
        region=region,
        request_body=GeminiRequestBody.model_validate({"contents": []}),
        response_body=GeminiResponse.model_validate({"candidates": [], "usageMetadata": {}}),
        api_method=api_method,
        request_latency_ms=request_latency_ms,
    )


def _mk_audit(
    *,
    timestamp: datetime,
    region: str = "us-central1",
    model: str = "publishers/google/models/gemini-2.5-flash",
    method: str = "google.cloud.aiplatform.v1.PredictionService.GenerateContent",
    principal_email: str | None = "alice@example.com",
    principal_subject: str | None = "user:alice@example.com",
    oauth_client_id: str | None = "32555940559.apps.googleusercontent.com",
    delegation: list = (),  # type: ignore[type-arg]
):
    from slashid_vertex_forwarder.audit_source import AuditEntry, DelegationHop

    return AuditEntry(
        timestamp=timestamp,
        resource_name=f"projects/p/locations/{region}/{model}",
        method_name=method,
        effective_principal_email=principal_email,
        effective_principal_subject=principal_subject,
        effective_oauth_client_id=oauth_client_id,
        delegation_chain=[
            DelegationHop(principal_subject=d.get("subject"), first_party_email=d.get("email"))
            for d in delegation
        ],
    )


def test_predict_audit_ts_applies_bias_and_latency() -> None:
    """predicted = logging_time - latency + BIAS(50ms)."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts

    entry = _mk_entry(
        logging_time=datetime(2026, 9, 9, 12, 0, 3, 0, tzinfo=UTC),
        request_latency_ms=2000.0,
    )
    predicted = _predict_audit_ts(entry)
    # 12:00:03 - 2s + 50ms = 12:00:01.050
    assert predicted == datetime(2026, 9, 9, 12, 0, 1, 50000, tzinfo=UTC)


def test_predict_audit_ts_no_latency_defaults_to_bias_only() -> None:
    """When latency is None, only the +50ms bias is applied."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts

    entry = _mk_entry(
        logging_time=datetime(2026, 9, 9, 12, 0, 3, 0, tzinfo=UTC),
        request_latency_ms=None,
    )
    predicted = _predict_audit_ts(entry)
    assert predicted == datetime(2026, 9, 9, 12, 0, 3, 50000, tzinfo=UTC)


def test_consensus_agrees_returns_value() -> None:
    from slashid_vertex_forwarder.event_source import _consensus

    assert _consensus({"a"}) == "a"
    assert _consensus({None}) is None


def test_consensus_disagrees_returns_none() -> None:
    from slashid_vertex_forwarder.event_source import _consensus

    assert _consensus({"a", "b"}) is None
    assert _consensus({"a", None}) is None  # None counts as a value


def test_credential_chain_length_1_from_direct_user() -> None:
    """Direct user call: chain=[effective], oauth on [0]."""
    from slashid_vertex_forwarder.event_source import _credential_chain

    a = _mk_audit(timestamp=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC))
    chain = _credential_chain(a)
    assert len(chain) == 1
    assert chain[0].principal_email == "alice@example.com"
    assert chain[0].principal_subject == "user:alice@example.com"
    assert chain[0].oauth_client_id == "32555940559.apps.googleusercontent.com"


def test_credential_chain_length_2_from_impersonation_1_hop() -> None:
    """alice -> sa: delegation hop at [0], effective at [1]. oauth on [0]."""
    from slashid_vertex_forwarder.event_source import _credential_chain

    a = _mk_audit(
        timestamp=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
        principal_email="sa@proj.iam.gserviceaccount.com",
        principal_subject="serviceAccount:sa@proj.iam.gserviceaccount.com",
        oauth_client_id=None,  # SA tokens aren't OAuth
        delegation=[{"subject": "user:alice@example.com", "email": "alice@example.com"}],
    )
    chain = _credential_chain(a)
    assert len(chain) == 2
    assert chain[0].principal_email == "alice@example.com"
    assert chain[0].principal_subject == "user:alice@example.com"
    assert chain[0].oauth_client_id is None  # oauth was None on the entry
    assert chain[1].principal_email == "sa@proj.iam.gserviceaccount.com"
    assert chain[1].principal_subject.startswith("serviceAccount:")


def test_credential_chain_length_3_from_2_hop_impersonation() -> None:
    from slashid_vertex_forwarder.event_source import _credential_chain

    a = _mk_audit(
        timestamp=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
        principal_email="sa2@proj.iam.gserviceaccount.com",
        principal_subject="serviceAccount:sa2@proj.iam.gserviceaccount.com",
        oauth_client_id=None,
        delegation=[
            {"subject": "user:alice@example.com", "email": "alice@example.com"},
            {
                "subject": "serviceAccount:sa1@proj.iam.gserviceaccount.com",
                "email": "sa1@proj.iam.gserviceaccount.com",
            },
        ],
    )
    chain = _credential_chain(a)
    assert len(chain) == 3
    assert chain[0].principal_email == "alice@example.com"
    assert chain[1].principal_email == "sa1@proj.iam.gserviceaccount.com"
    assert chain[2].principal_email == "sa2@proj.iam.gserviceaccount.com"


def test_credential_chain_attaches_oauth_only_to_root() -> None:
    """Direct human call: oauth lands on chain[0] (which equals chain[-1])."""
    from slashid_vertex_forwarder.event_source import _credential_chain

    a = _mk_audit(
        timestamp=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
        oauth_client_id="oauth-abc",
    )
    chain = _credential_chain(a)
    assert chain[0].oauth_client_id == "oauth-abc"


def test_resolve_identity_no_candidates_returns_empty() -> None:
    """No matching audit entries in window -> credential_chain=None."""
    from slashid_vertex_forwarder.event_source import _resolve_identity

    entry = _mk_entry()
    result = _resolve_identity(entry, [])
    assert result.credential_chain is None


def test_resolve_identity_single_candidate_populates_chain() -> None:
    """One matching audit entry -> chain from that entry."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    audit = _mk_audit(timestamp=predicted)
    result = _resolve_identity(entry, [audit])
    assert result.credential_chain is not None
    assert len(result.credential_chain) == 1
    assert result.credential_chain[0].principal_email == "alice@example.com"


def test_resolve_identity_two_candidates_same_chain() -> None:
    """Two candidates agreeing on everything -> same chain."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    a1 = _mk_audit(timestamp=predicted - timedelta(milliseconds=10))
    a2 = _mk_audit(timestamp=predicted + timedelta(milliseconds=10))
    result = _resolve_identity(entry, [a1, a2])
    assert result.credential_chain is not None
    assert result.credential_chain[0].principal_email == "alice@example.com"


def test_resolve_identity_agreeing_principal_disagreeing_oauth() -> None:
    """Two candidates: same principal, different oauth_client_id -> principal
    fields kept, oauth dropped."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    a1 = _mk_audit(timestamp=predicted, oauth_client_id="client-A")
    a2 = _mk_audit(timestamp=predicted + timedelta(milliseconds=20), oauth_client_id="client-B")
    result = _resolve_identity(entry, [a1, a2])
    assert result.credential_chain is not None
    assert result.credential_chain[0].principal_email == "alice@example.com"
    assert result.credential_chain[0].oauth_client_id is None


def test_resolve_identity_disagreeing_principal_returns_none() -> None:
    """Two candidates disagreeing on principal -> both endpoints empty -> None."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    a1 = _mk_audit(
        timestamp=predicted,
        principal_email="alice@example.com",
        principal_subject="user:alice@example.com",
        oauth_client_id=None,
    )
    a2 = _mk_audit(
        timestamp=predicted + timedelta(milliseconds=20),
        principal_email="bob@example.com",
        principal_subject="user:bob@example.com",
        oauth_client_id=None,
    )
    result = _resolve_identity(entry, [a1, a2])
    assert result.credential_chain is None


def test_resolve_identity_out_of_window_candidate_ignored() -> None:
    """Audit entry present but outside +/-200ms of predicted -> no candidates."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    a = _mk_audit(timestamp=predicted + timedelta(milliseconds=500))  # way out
    result = _resolve_identity(entry, [a])
    assert result.credential_chain is None


def test_resolve_identity_wrong_region_ignored() -> None:
    """Same model, different region -> not a candidate."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry(region="us-central1")
    predicted = _predict_audit_ts(entry)
    a = _mk_audit(timestamp=predicted, region="us-east1")  # wrong region
    result = _resolve_identity(entry, [a])
    assert result.credential_chain is None


def test_resolve_identity_wrong_model_ignored() -> None:
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry(model_path="publishers/google/models/gemini-2.5-flash")
    predicted = _predict_audit_ts(entry)
    a = _mk_audit(timestamp=predicted, model="publishers/google/models/gemini-2.5-pro")
    result = _resolve_identity(entry, [a])
    assert result.credential_chain is None


def test_resolve_identity_wrong_method_ignored() -> None:
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry(api_method="GenerateContent")
    predicted = _predict_audit_ts(entry)
    a = _mk_audit(
        timestamp=predicted,
        method="google.cloud.aiplatform.v1.PredictionService.StreamGenerateContent",
    )
    result = _resolve_identity(entry, [a])
    assert result.credential_chain is None


def test_resolve_identity_length_mismatch_normalizes_to_root_effective() -> None:
    """Different chain depths but same root+effective -> length-2 chain."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    # A: alice -> sa (length 2)
    a1 = _mk_audit(
        timestamp=predicted,
        principal_email="sa@proj.iam.gserviceaccount.com",
        principal_subject="serviceAccount:sa@proj.iam.gserviceaccount.com",
        oauth_client_id=None,
        delegation=[{"subject": "user:alice@example.com", "email": "alice@example.com"}],
    )
    # B: alice -> mid -> sa (length 3)
    a2 = _mk_audit(
        timestamp=predicted + timedelta(milliseconds=20),
        principal_email="sa@proj.iam.gserviceaccount.com",
        principal_subject="serviceAccount:sa@proj.iam.gserviceaccount.com",
        oauth_client_id=None,
        delegation=[
            {"subject": "user:alice@example.com", "email": "alice@example.com"},
            {
                "subject": "serviceAccount:mid@proj.iam.gserviceaccount.com",
                "email": "mid@proj.iam.gserviceaccount.com",
            },
        ],
    )
    result = _resolve_identity(entry, [a1, a2])
    assert result.credential_chain is not None
    assert len(result.credential_chain) == 2
    assert result.credential_chain[0].principal_email == "alice@example.com"
    assert result.credential_chain[1].principal_email == "sa@proj.iam.gserviceaccount.com"


def test_resolve_identity_length_mismatch_different_effective_returns_none() -> None:
    """Different chain depths AND disagreeing endpoints -> both empty -> None."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    a1 = _mk_audit(
        timestamp=predicted,
        principal_email="sa1@proj.iam.gserviceaccount.com",
        principal_subject="serviceAccount:sa1@proj.iam.gserviceaccount.com",
        oauth_client_id=None,
        delegation=[{"subject": "user:alice@example.com", "email": "alice@example.com"}],
    )
    a2 = _mk_audit(
        timestamp=predicted + timedelta(milliseconds=20),
        principal_email="sa2@proj.iam.gserviceaccount.com",
        principal_subject="serviceAccount:sa2@proj.iam.gserviceaccount.com",
        oauth_client_id=None,
        delegation=[
            {"subject": "user:bob@example.com", "email": "bob@example.com"},
            {
                "subject": "serviceAccount:mid@proj.iam.gserviceaccount.com",
                "email": "mid@proj.iam.gserviceaccount.com",
            },
        ],
    )
    result = _resolve_identity(entry, [a1, a2])
    assert result.credential_chain is None


def test_resolve_identity_length_1_mixed_with_length_2_partial_attribution() -> None:
    """Direct alice + impersonated alice->sa: normalized to length-2, position [0]
    agrees on alice, [-1] disagrees (alice vs sa) -> tail empty, root populated."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    # A: direct alice (length 1)
    a1 = _mk_audit(timestamp=predicted, oauth_client_id="oauth-a")
    # B: alice -> sa (length 2)
    a2 = _mk_audit(
        timestamp=predicted + timedelta(milliseconds=20),
        principal_email="sa@proj.iam.gserviceaccount.com",
        principal_subject="serviceAccount:sa@proj.iam.gserviceaccount.com",
        oauth_client_id=None,
        delegation=[{"subject": "user:alice@example.com", "email": "alice@example.com"}],
    )
    result = _resolve_identity(entry, [a1, a2])
    assert result.credential_chain is not None
    assert len(result.credential_chain) == 2
    assert result.credential_chain[0].principal_email == "alice@example.com"
    # Effective disagrees -> all fields None on [1]
    assert result.credential_chain[1].principal_email is None
    assert result.credential_chain[1].principal_subject is None
    assert result.credential_chain[1].oauth_client_id is None


def test_fetch_stamps_identity_details_on_entries(monkeypatch) -> None:
    """End-to-end: fetch queries payload, queries audit, stamps identity
    on each returned Entry. All sync."""
    from slashid_vertex_forwarder.event_source import BqEventSource, Checkpoint

    bq_client = MagicMock()
    bq_client.query.return_value.result.return_value = [_minimal_row()]

    predicted_row_time = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    matching_audit = _mk_audit(
        timestamp=predicted_row_time + timedelta(milliseconds=50),
    )

    monkeypatch.setattr(
        BqEventSource, "_query_audit", lambda self, ts_range: [matching_audit]
    )

    source = BqEventSource(
        client=bq_client,
        project_id="p",
        dataset_id="d",
        region="us-central1",
        max_rows_per_tick=100,
        audit_buffer_seconds=0,
    )
    entries = source.fetch(Checkpoint(None, None))
    assert len(entries) == 1
    chain = entries[0].identity_details.credential_chain
    assert chain is not None
    assert chain[0].principal_email == "alice@example.com"


def test_fetch_ts_range_uses_predicted_bounds_with_window_slack(monkeypatch) -> None:
    """The audit query's ts_range is [min_predicted - _WINDOW, max_predicted
    + _WINDOW] — computed per row."""
    from slashid_vertex_forwarder.event_source import BqEventSource, Checkpoint, _WINDOW

    bq_client = MagicMock()
    row1 = _minimal_row(
        request_id=1,
        logging_time=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
        metadata={"request_latency": 0.0},
    )
    row2 = _minimal_row(
        request_id=2,
        logging_time=datetime(2026, 9, 9, 12, 0, 5, tzinfo=UTC),
        metadata={"request_latency": 0.0},
    )
    bq_client.query.return_value.result.return_value = [row1, row2]

    captured: list[tuple] = []

    def _capture(self, ts_range):
        captured.append(ts_range)
        return []

    monkeypatch.setattr(BqEventSource, "_query_audit", _capture)

    source = BqEventSource(
        client=bq_client,
        project_id="p",
        dataset_id="d",
        region="us-central1",
        max_rows_per_tick=100,
        audit_buffer_seconds=0,
    )
    source.fetch(Checkpoint(None, None))
    lo, hi = captured[0]
    assert lo == datetime(2026, 9, 9, 12, 0, 0, 50000, tzinfo=UTC) - _WINDOW
    assert hi == datetime(2026, 9, 9, 12, 0, 5, 50000, tzinfo=UTC) + _WINDOW


def test_fetch_empty_rows_skips_audit_query(monkeypatch) -> None:
    """No BQ rows → no audit query fired."""
    from slashid_vertex_forwarder.event_source import BqEventSource, Checkpoint

    bq_client = MagicMock()
    bq_client.query.return_value.result.return_value = []

    called = {"n": 0}

    def _fail_if_called(self, ts_range):
        called["n"] += 1
        return []

    monkeypatch.setattr(BqEventSource, "_query_audit", _fail_if_called)

    source = BqEventSource(
        client=bq_client,
        project_id="p",
        dataset_id="d",
        region="us-central1",
        max_rows_per_tick=100,
        audit_buffer_seconds=0,
    )
    entries = source.fetch(Checkpoint(None, None))
    assert entries == []
    assert called["n"] == 0


def test_resolve_identity_audit_entries_out_of_order_still_works() -> None:
    """Bisect assumes sorted input. Sort explicitly before calling."""
    from slashid_vertex_forwarder.event_source import _predict_audit_ts, _resolve_identity

    entry = _mk_entry()
    predicted = _predict_audit_ts(entry)
    # In-window candidates in sorted order
    a1 = _mk_audit(timestamp=predicted - timedelta(milliseconds=50))
    a2 = _mk_audit(timestamp=predicted + timedelta(milliseconds=50))
    # Out-of-window early and late
    outside_early = _mk_audit(timestamp=predicted - timedelta(seconds=5))
    outside_late = _mk_audit(timestamp=predicted + timedelta(seconds=5))
    audit_entries = sorted(
        [a1, a2, outside_early, outside_late], key=lambda x: x.timestamp
    )
    result = _resolve_identity(entry, audit_entries)
    assert result.credential_chain is not None
