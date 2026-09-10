"""Polled ``EventSource`` protocol + BigQuery-backed implementation.

BigQuery request-response logging (enabled per publisher model via
``setPublisherModelConfig``) writes each Vertex ``generateContent`` call
to a per-model table with the shape documented at
https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/request-response-logging.
This module polls those tables on each Cloud Scheduler tick.

The abstraction (``EventSource`` protocol + ``Entry`` dataclass) keeps
the handler loop source-agnostic — a later phase can swap in a joined
BQ view or a Pub/Sub push subscription behind the same interface.

Checkpoint format: ``(timestamp, id)``. The BQ query filters
``logging_time > timestamp`` OR (equal AND ``request_id > id``).
Boundary collisions are safe — the server dedupes on ``request_id``.
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from slashid_ai_forwarder_core.events import GCPCredential, GCPIdentityDetails
from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiRequestBody,
    GeminiResponse,
)

from .audit_source import AuditEntry

log = logging.getLogger(__name__)


# --- Identity-correlation join (see identity-correlation design doc) --------

_WINDOW = timedelta(milliseconds=200)
_BIAS = timedelta(milliseconds=50)  # audit is ~50ms LATER than predicted


def _predict_audit_ts(row: Entry) -> datetime:
    """Predicted audit-log timestamp for a BQ payload row: rolls back
    from ``logging_time`` by ``request_latency_ms`` and adds the fixed
    +50ms clock/write-skew bias observed in the benchmark."""
    latency = timedelta(milliseconds=row.request_latency_ms or 0)
    return row.logging_time - latency + _BIAS


def _consensus(vals: set[str | None]) -> str | None:
    """Return the sole value everyone agrees on; None on disagreement.
    ``None`` counts as a value — mixed None/populated is disagreement."""
    return next(iter(vals)) if len(vals) == 1 else None


def _credential_chain(a: AuditEntry) -> list[GCPCredential]:
    """Reconstruct the full credential chain from one audit entry:
    delegation hops (root at [0]) + effective principal (at [-1]).

    ``oauth_client_id`` from ``authenticationInfo.oauthInfo`` describes
    the token that authenticated THIS request — that's the effective
    credential (chain[-1]). For non-impersonated calls chain[0] ==
    chain[-1] so both interpretations coincide; for impersonated calls
    the value uniquely identifies the effective SA's OAuth flow (a
    numeric ID for SA-issued tokens, or the CLI's registered client ID
    for direct user calls). The audit log does not preserve the
    ROOT's OAuth flow across impersonation hops, so ``chain[0]``
    remains oauth-less."""
    chain = [
        GCPCredential(
            principal_email=hop.first_party_email,
            principal_subject=hop.principal_subject,
        )
        for hop in a.delegation_chain
    ]
    chain.append(
        GCPCredential(
            principal_email=a.effective_principal_email,
            principal_subject=a.effective_principal_subject,
            oauth_client_id=a.effective_oauth_client_id,
        )
    )
    return chain


def _consensus_chain(
    candidates: list[AuditEntry],
) -> list[GCPCredential] | None:
    """Return the credential chain every candidate agrees on. When
    candidates agree on chain length, per-position per-field consensus
    over the full chain. When lengths differ, fall back to normalizing
    every chain to ``[root, effective]`` and consensus on that.

    Returns None only when BOTH endpoints (chain[0] and chain[-1]) are
    empty after consensus. Partial results (one endpoint populated, the
    other empty) still ship.
    """
    if not candidates:
        return None
    chains = [_credential_chain(c) for c in candidates]
    lengths = {len(ch) for ch in chains}
    if len(lengths) != 1:
        # Length mismatch → normalize to [root, effective] (2-entry chain).
        # For a length-1 original, ch[0] == ch[-1] and carries oauth on the
        # tail slot. When we duplicate that credential into the root slot
        # of the normalized pair, strip ``oauth_client_id`` off it —
        # ``oauth_client_id`` semantically belongs only to the effective
        # position, and consensus across mismatched chains would otherwise
        # leak the effective's oauth into the normalized root.
        chains = [[ch[0].model_copy(update={"oauth_client_id": None}), ch[-1]] for ch in chains]
    n = len(chains[0])
    result = [
        GCPCredential(
            principal_email=_consensus({ch[i].principal_email for ch in chains}),
            principal_subject=_consensus({ch[i].principal_subject for ch in chains}),
            oauth_client_id=_consensus({ch[i].oauth_client_id for ch in chains}),
        )
        for i in range(n)
    ]
    fields = ("principal_email", "principal_subject", "oauth_client_id")
    root_empty = all(getattr(result[0], f) is None for f in fields)
    tail_empty = all(getattr(result[-1], f) is None for f in fields)
    if root_empty and tail_empty:
        return None
    return result


def _resolve_identity(row: Entry, audit_entries: list[AuditEntry]) -> GCPIdentityDetails:
    """``audit_entries`` MUST be sorted ascending by timestamp — the
    caller queries the log API with ``order_by="timestamp asc"``. We
    bisect the sorted list to slice the time window in O(log N)."""
    predicted = _predict_audit_ts(row)
    i = bisect_left(audit_entries, predicted - _WINDOW, key=lambda a: a.timestamp)
    j = bisect_right(audit_entries, predicted + _WINDOW, key=lambda a: a.timestamp)

    method_suffix = row.api_method
    resource_suffix = f"/locations/{row.region}/{row.model_path}"
    candidates = [
        a
        for a in audit_entries[i:j]
        if a.method_name.endswith("." + method_suffix) and a.resource_name.endswith(resource_suffix)
    ]
    return GCPIdentityDetails(credential_chain=_consensus_chain(candidates))


@dataclass(frozen=True)
class Checkpoint:
    """The polling watermark — ``(timestamp, id)`` of the last processed
    entry. Universal across event sources.

    ``timestamp = None`` means "no entries seen yet"; the source fetches
    every entry up to its batch bound.
    """

    timestamp: datetime | None
    id: str | None


@dataclass
class Entry:
    """One row from a BQ request-response logging table.

    ``request_body`` / ``response_body`` are typed pydantic — the source
    validates raw JSON at fetch time so the handler pipeline gets
    canonical inputs. ``model_path`` is the full publisher model path
    (``publishers/google/models/gemini-2.5-flash``) — used as-is for
    ``AIModel.id``.

    ``api_method`` is Vertex's ``GenerateContent`` / ``StreamGenerateContent``
    label (from the BQ table's ``api_method`` column). Used to disambiguate
    streaming vs non-streaming in the audit-log join.

    ``request_latency_ms`` is Vertex's per-call latency in milliseconds
    (from the ``metadata.request_latency`` BQ field). Feeds the
    identity-correlation join's audit-timestamp prediction.

    ``identity_details`` is stamped by the source's audit-log join
    after construction — defaults to an empty ``GCPIdentityDetails()``.
    Entry is intentionally not frozen so the join can attach identity.
    """

    request_id: str
    logging_time: datetime
    model_path: str
    region: str
    request_body: GeminiRequestBody
    response_body: GeminiResponse
    api_method: str  # REQUIRED — Vertex BQ populates it on every row;
    # empty string would match every audit entry's method_name suffix
    # (``.endswith(".")`` heuristic) so we don't let it default.
    request_latency_ms: float | None = None
    identity_details: GCPIdentityDetails = field(default_factory=GCPIdentityDetails)

    @property
    def checkpoint(self) -> Checkpoint:
        """Checkpoint pointing at this entry — save after successful push."""
        return Checkpoint(
            timestamp=self.logging_time,
            id=self.request_id,
        )


class EventSource(Protocol):
    """Fetch the next batch of Vertex invocations past ``checkpoint``.

    Implementations return an ordered list (ascending by
    ``(logging_time, request_id)``) — the handler saves the last
    entry's ``.checkpoint`` after a successful push cycle. Empty list
    on no new rows.
    """

    def fetch(self, checkpoint: Checkpoint) -> list[Entry]: ...


class BqEventSource:
    """Polls a BigQuery dataset for new Vertex request-response logs.

    Reads across every table in ``dataset_id`` — Terraform provisions
    one table per logged publisher model, and this source pulls the
    union so the handler doesn't need to enumerate models. Region is
    baked into the table name (``slashid_vertex_reqresp_<model_slug>``)
    but the BQ row itself doesn't carry region, so we thread it in from
    the config.
    """

    def __init__(
        self,
        *,
        # google.cloud.bigquery.Client — kept untyped so GCP client deps don't
        # bleed into the type-check surface (they're runtime-only).
        client: Any,
        project_id: str,
        dataset_id: str,
        region: str,
        max_rows_per_tick: int,
        audit_buffer_seconds: int = 0,
    ) -> None:
        self._client = client
        self._project_id = project_id
        self._dataset_id = dataset_id
        self._region = region
        self._max_rows_per_tick = max_rows_per_tick
        self._audit_buffer_seconds = audit_buffer_seconds

    def fetch(self, checkpoint: Checkpoint) -> list[Entry]:
        """Two-query orchestration: BQ payload → audit-log window →
        per-row identity stamping. Rows come back with
        ``identity_details`` populated (or empty if no consensus).

        Sync end-to-end. Cloud Logging's ``list_entries`` and BigQuery's
        ``job.result()`` both block anyway, so making the source layer
        async would be theatre.
        """
        query, params = self._build_query(checkpoint)
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(query_parameters=params)
        job = self._client.query(query, job_config=job_config)
        payload_rows: list[Entry] = []
        for row in job.result():
            entry = _row_to_entry(row, region=self._region)
            if entry is not None:
                payload_rows.append(entry)
        if not payload_rows:
            return []

        # Predict each row's audit timestamp first (bias-corrected), then
        # take the tight envelope + ±_WINDOW slack. Matches the per-row
        # window exactly — everything the filter lets through is a
        # potential candidate for at least one row.
        predicted = [_predict_audit_ts(r) for r in payload_rows]
        ts_range = (min(predicted) - _WINDOW, max(predicted) + _WINDOW)
        audit_entries = self._query_audit(ts_range)

        for row in payload_rows:
            row.identity_details = _resolve_identity(row, audit_entries)
        return payload_rows

    def _query_audit(
        self,
        ts_range: tuple[datetime, datetime],
    ) -> list[AuditEntry]:
        """Fetch matching Cloud Audit Log entries. Overridden in tests
        via monkeypatch.

        ``_use_grpc=False`` forces REST transport. The gRPC transport
        in ``google-cloud-logging`` drops several nested fields from
        the ``AuditLog`` protoPayload (empirically: ``oauthInfo``,
        ``serviceAccountDelegationInfo``) that we depend on for
        credential-chain reconstruction. REST returns the full
        LogEntry payload. See identity-correlation smoke findings
        (2026-09-09)."""
        from google.cloud import logging as gcp_logging

        from .audit_source import query_audit_entries

        client = gcp_logging.Client(project=self._project_id, _use_grpc=False)
        return query_audit_entries(
            client=client,
            project_id=self._project_id,
            region=self._region,
            ts_range=ts_range,
        )

    def _build_query(
        self,
        checkpoint: Checkpoint,
        *,
        now: datetime | None = None,
    ) -> tuple[str, list[Any]]:
        """Return the parameterised SQL + query-parameters for one tick.

        Reads every ``slashid_vertex_reqresp_*`` table in the dataset via
        a wildcard table reference. Filters:
          - ``logging_time`` + ``request_id`` ordering to progress past
            the checkpoint.
          - ``logging_time <= @cutoff`` (when ``audit_buffer_seconds > 0``)
            so payload rows only surface once Cloud Audit Logs have had
            time to land for the identity-correlation join.

        ``now`` is injected for deterministic tests; production callers
        pass ``None`` and get ``datetime.now(UTC)``.
        """
        from google.cloud import bigquery

        # Wildcard table read — one FROM covers every provisioned model
        # table without the handler having to know the model list.
        table_glob = f"`{self._project_id}.{self._dataset_id}.slashid_vertex_reqresp_*`"
        params: list[Any] = [
            bigquery.ScalarQueryParameter("limit", "INT64", self._max_rows_per_tick),
        ]
        where_clauses: list[str] = []
        if checkpoint.timestamp is not None and checkpoint.id is not None:
            where_clauses.append(
                "(logging_time > @cp_ts "
                "OR (logging_time = @cp_ts AND CAST(request_id AS STRING) > @cp_id))"
            )
            params.extend(
                [
                    bigquery.ScalarQueryParameter(
                        "cp_ts", "TIMESTAMP", checkpoint.timestamp
                    ),
                    bigquery.ScalarQueryParameter("cp_id", "STRING", checkpoint.id),
                ]
            )
        if self._audit_buffer_seconds > 0:
            cutoff = (now or datetime.now(UTC)) - timedelta(seconds=self._audit_buffer_seconds)
            where_clauses.append("logging_time <= @cutoff")
            params.append(bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff))
        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = (
            "SELECT request_id, logging_time, model, api_method, metadata, "
            "full_request, full_response "
            f"FROM {table_glob} "
            f"{where} "
            "ORDER BY logging_time ASC, CAST(request_id AS STRING) ASC "
            "LIMIT @limit"
        )
        return query, params


def _row_to_entry(row: Any, *, region: str) -> Entry | None:
    """Validate a BQ row into an ``Entry`` — best-effort, drop on parse failure.

    BQ hands us ``request_id`` as an integer (NUMERIC in the source
    schema) and ``full_request`` / ``full_response`` as ``JSON`` columns
    that surface either as dicts or JSON-encoded strings depending on
    driver version; both shapes are accepted.

    ``row`` is a ``google.cloud.bigquery.table.Row`` in production and a
    plain dict in the fake-client tests — both expose ``.get(key)``.
    """
    import json

    from pydantic import ValidationError

    request_id = row.get("request_id")
    logging_time = row.get("logging_time")
    model = row.get("model")
    req_raw = row.get("full_request")
    resp_raw = row.get("full_response")

    if request_id is None or logging_time is None or model is None:
        missing = [
            name
            for name, value in (
                ("request_id", request_id),
                ("logging_time", logging_time),
                ("model", model),
            )
            if value is None
        ]
        log.warning(
            "dropping BQ row with missing required fields: %s (request_id=%r)",
            ",".join(missing),
            request_id,
        )
        return None

    req_dict = json.loads(req_raw) if isinstance(req_raw, str) else req_raw
    resp_dict = json.loads(resp_raw) if isinstance(resp_raw, str) else resp_raw
    if not isinstance(req_dict, dict) or not isinstance(resp_dict, dict):
        log.warning(
            "dropping BQ row with non-dict payload: request_id=%s full_request=%s full_response=%s",
            request_id,
            type(req_dict).__name__,
            type(resp_dict).__name__,
        )
        return None

    try:
        request_body = GeminiRequestBody.model_validate(req_dict)
        response_body = GeminiResponse.model_validate(resp_dict)
    except ValidationError as e:
        log.warning(
            "dropping BQ row with schema-invalid payload: request_id=%s error=%s",
            request_id,
            e,
        )
        return None

    api_method = str(row.get("api_method") or "")
    request_latency_ms = _parse_latency_ms(row.get("metadata"))

    return Entry(
        request_id=str(request_id),
        logging_time=logging_time,
        model_path=str(model),
        region=region,
        request_body=request_body,
        response_body=response_body,
        api_method=api_method,
        request_latency_ms=request_latency_ms,
    )


def _parse_latency_ms(metadata: Any) -> float | None:
    """Parse ``metadata.request_latency`` from the BQ ``metadata`` column.

    Column is JSON-typed; drivers may hand it back as a dict or as a
    JSON-encoded string. Value is a bare float in milliseconds (verified
    empirically in the identity-correlation phase's benchmark).
    Returns None on any parse failure — join still works with a wider
    effective window in that case.
    """
    import json

    if metadata is None:
        return None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("request_latency")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
