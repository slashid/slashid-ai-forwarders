"""Tests for ``BqEventSource`` — polls a BigQuery wildcard-table union.

Uses a fake ``bigquery.Client`` doubles that captures the issued query
+ params and returns pre-built rows. The real client is heavyweight
(auth, discovery, retry) and covered by GCP integration tests outside
this suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from slashid_vertex_forwarder.event_source import BqEventSource, Checkpoint


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
    request_payload: dict[str, Any] | str | None = None,
    response_payload: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "logging_time": logging_time or datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC),
        "model": model,
        "full_request": request_payload or _POC_REQ,
        "full_response": response_payload or _POC_RESP,
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
    assert "WHERE logging_time > @last_time" in q
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
