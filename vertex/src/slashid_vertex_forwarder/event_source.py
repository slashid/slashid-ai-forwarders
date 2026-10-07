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

import asyncio
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    GCPCredential,
    GCPIdentityDetails,
)
from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiRequestBody,
    GeminiResponse,
)
from slashid_ai_forwarder_core.platform import Checkpoint, CheckpointStore, load_or_start

from .audit_source import AuditEntry, _credential_chain

if TYPE_CHECKING:
    from google.cloud.bigquery import Client as BigQueryClient
    from google.cloud.logging import Client as LoggingClient

    from .config import Config

log = logging.getLogger(__name__)


# --- Identity-correlation join (see identity-correlation design doc) --------

# Correlation offsets, measured over 530 tagged calls on 2026-09-17.
# The audit entry consistently PRECEDES ``logging_time - latency``, so the
# bias is subtracted, pulling the prediction back onto it. Only two factors move the
# offset: global vs regional routing, and unary vs streaming. Dataset
# storage region, payload size and model tier were all measured and do
# not. Windows carry ~100ms of extra slack because the offset drifts
# across the day by about that much; see ~/vertex-corr-bench.
_REGIONAL = (timedelta(milliseconds=130), timedelta(milliseconds=200))
_GLOBAL_STREAM = (timedelta(milliseconds=175), timedelta(milliseconds=250))
_GLOBAL_UNARY = (timedelta(milliseconds=445), timedelta(milliseconds=300))


def _correlation(row: Entry) -> tuple[timedelta, timedelta]:
    """``(bias, window)`` for ``row``'s bucket.

    Regional traffic uses one pair for both methods — the two differ by
    less than their own spread. Global does not: a non-streaming global
    call sits ~270ms further out than a streaming one, enough that a
    shared bias misses every time."""
    if row.region != "global":
        return _REGIONAL
    return _GLOBAL_STREAM if row.api_method.startswith("Stream") else _GLOBAL_UNARY


def _predict_audit_ts(row: Entry) -> datetime:
    """Predicted audit-log timestamp for a BQ payload row: rolls back
    from ``logging_time`` by ``request_latency_ms``, then by the bucket's
    bias, since the audit entry lands before that point."""
    latency = timedelta(milliseconds=row.request_latency_ms or 0)
    return row.logging_time - latency - _correlation(row)[0]


def _consensus(vals: set[str | None]) -> str | None:
    """Return the sole value everyone agrees on; None on disagreement.
    ``None`` counts as a value — mixed None/populated is disagreement."""
    return next(iter(vals)) if len(vals) == 1 else None


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


def _audit_candidates(row: Entry, audit_entries: list[AuditEntry]) -> list[AuditEntry]:
    """Audit entries that could correspond to ``row``.

    ``audit_entries`` MUST be sorted ascending by timestamp — the
    caller queries the log API with ``order_by="timestamp asc"``. We
    bisect the sorted list to slice the time window in O(log N), then
    narrow by method + model path."""
    predicted = _predict_audit_ts(row)
    window = _correlation(row)[1]
    i = bisect_left(audit_entries, predicted - window, key=lambda a: a.timestamp)
    j = bisect_right(audit_entries, predicted + window, key=lambda a: a.timestamp)

    method_suffix = row.api_method
    resource_suffix = f"/locations/{row.region}/{row.model_path}"
    return [
        a
        for a in audit_entries[i:j]
        if a.method_name.endswith("." + method_suffix) and a.resource_name.endswith(resource_suffix)
    ]


def _resolve_identity(row: Entry, audit_entries: list[AuditEntry]) -> GCPIdentityDetails:
    """Per-field consensus identity across every matching audit entry."""
    return GCPIdentityDetails(
        credential_chain=_consensus_chain(_audit_candidates(row, audit_entries))
    )


def _resolve_user_agent(row: Entry, audit_entries: list[AuditEntry]) -> str | None:
    """The user agent every matching audit entry agrees on.

    Same consensus rule as the credential chain: on multi-tenant
    ambiguity (two callers hitting the same model + method inside the
    correlation window) disagreement collapses to ``None`` rather than
    attributing one caller's client to another's invocation."""
    return _consensus({a.user_agent for a in _audit_candidates(row, audit_entries)})


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
    # Stamped by the same audit-log join that resolves identity — BQ
    # payload rows carry no user-agent of their own.
    user_agent: str | None = None

    @property
    def checkpoint(self) -> Checkpoint:
        """Checkpoint pointing at this entry — save after successful push."""
        return Checkpoint(
            timestamp=self.logging_time,
            id=self.request_id,
        )


class EventSource(Protocol):
    """Yield wire-ready ``AIInvocationObservedV1`` events past this
    source's checkpoint.

    Each implementation owns its own ``CheckpointStore`` via its
    constructor AND owns its full source-specific pipeline —
    entry parsing, normalize, finalize, envelope construction, and
    the ``build_event_from_normalized`` join. The handler treats
    sources uniformly — it only sees ``AIInvocationObservedV1``.
    Entry types stay private to their source.

    ``fetch()`` loads the checkpoint internally and returns a
    ``(events, next_checkpoint)`` tuple:

    - ``events`` is the list of ``AIInvocationObservedV1`` objects
      to push to the wire (ordered by the source's underlying
      ``(timestamp, id)`` ascending order).
    - ``next_checkpoint`` is the watermark to save after successful
      push. ``None`` means "no raw records seen this tick" (checkpoint
      stays put). A non-None value advances past raw records the source
      saw even when they were dropped during parsing — so a permanent
      parse failure doesn't stall the pipeline.

    ``commit(checkpoint)`` saves the watermark; called by the handler
    after successful wire push. Never called when ``next_checkpoint``
    is ``None``.
    """

    async def fetch(self) -> tuple[list[AIInvocationObservedV1], Checkpoint | None]: ...
    async def commit(self, checkpoint: Checkpoint) -> None: ...


@dataclass(frozen=True)
class _Scan:
    """What one pass over the BigQuery rows found."""

    rows: list[Entry]
    raw_seen: int
    max_ts: datetime | None
    max_id: str | None


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
        client: BigQueryClient,
        checkpoint_store: CheckpointStore,
        config: Config,
        project_id: str,
        dataset_id: str,
        region: str,
        max_rows_per_tick: int,
        audit_buffer_seconds: int = 0,
    ) -> None:
        self._client = client
        self._checkpoint_store = checkpoint_store
        self._config = config
        self._project_id = project_id
        self._dataset_id = dataset_id
        self._region = region
        self._max_rows_per_tick = max_rows_per_tick
        self._audit_buffer_seconds = audit_buffer_seconds
        self._logging_client: LoggingClient | None = None

    def _read_payload_rows(self, checkpoint: Checkpoint) -> _Scan:
        """The blocking BigQuery half: run the query and walk every raw row,
        tracking the max ``(timestamp, id)`` across **all** of them, even the
        ones ``_row_to_entry`` drops, so a permanent parse failure cannot
        stall the checkpoint."""
        from google.cloud import bigquery

        query, params = self._build_query(checkpoint)
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        job = self._client.query(query, job_config=job_config)

        payload_rows: list[Entry] = []
        max_ts: datetime | None = None
        max_id: str | None = None
        raw_seen = 0
        for row in job.result():
            raw_seen += 1
            row_ts = row.get("logging_time")
            row_id = row.get("request_id")
            if row_ts is not None and row_id is not None:
                row_id_str = str(row_id)
                if max_ts is None or (row_ts, row_id_str) > (
                    max_ts,
                    max_id or "",
                ):
                    max_ts = row_ts
                    max_id = row_id_str

            entry = _row_to_entry(row, region=self._region)
            if entry is not None:
                payload_rows.append(entry)
        return _Scan(payload_rows, raw_seen, max_ts, max_id)

    async def fetch(self) -> tuple[list[AIInvocationObservedV1], Checkpoint | None]:
        """Two-query orchestration: BQ payload → audit-log window →
        per-row identity stamping → envelope construction → normalize →
        finalize → build final wire event. Returns
        (events, next_checkpoint).

        ``next_checkpoint`` reflects the max ``(logging_time, request_id)``
        across ALL raw BQ rows — including rows that ``_row_to_entry``
        drops as unparseable — so permanent parse failures do not stall
        the pipeline. ``None`` on zero raw rows.

        The BigQuery and Cloud Logging calls block, so they run in a worker
        thread; the Gemini normalize/finalize/build_event half is async.
        """
        # An empty id: ``_build_query`` only applies a checkpoint that has one.
        checkpoint = await load_or_start(self._checkpoint_store, now=datetime.now(UTC), id="")
        scan = await asyncio.to_thread(self._read_payload_rows, checkpoint)
        if scan.raw_seen == 0:
            return [], None

        next_checkpoint = Checkpoint(timestamp=scan.max_ts, id=scan.max_id)

        if not scan.rows:
            return [], next_checkpoint

        # Predict each row's audit timestamp first (bias-corrected), then
        # take the envelope of each row's own window. Matches the per-row
        # window exactly — everything the filter lets through is a
        # potential candidate for at least one row.
        spans = [(_predict_audit_ts(r), _correlation(r)[1]) for r in scan.rows]
        ts_range = (min(p - w for p, w in spans), max(p + w for p, w in spans))
        audit_entries = await asyncio.to_thread(self._query_audit, ts_range)

        for row in scan.rows:
            row.identity_details = _resolve_identity(row, audit_entries)
            row.user_agent = _resolve_user_agent(row, audit_entries)

        events = await _gemini_pipeline(scan.rows, self._config)
        return events, next_checkpoint

    async def commit(self, checkpoint: Checkpoint) -> None:
        """Advance the source's checkpoint. Called by the handler after
        successful wire push."""
        await self._checkpoint_store.save(checkpoint)

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

        if self._logging_client is None:
            self._logging_client = gcp_logging.Client(project=self._project_id, _use_grpc=False)
        return query_audit_entries(
            client=self._logging_client,
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
                    bigquery.ScalarQueryParameter("cp_ts", "TIMESTAMP", checkpoint.timestamp),
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


async def _gemini_pipeline(
    entries: list[Entry],
    config: Config,
) -> list[AIInvocationObservedV1]:
    """Async Gemini-specific half of the BQ source pipeline.

    For each parsed BQ ``Entry``: normalize the Gemini request/response
    into the canonical ``NormalizedInvocation`` shape, finalize (in-place
    post-processing — attachment hashing, accessed-file extraction,
    etc.), then build the ``EventEnvelope`` and combine with the
    normalized invocation into a final ``AIInvocationObservedV1``.

    Runs the normalize+finalize step concurrently across entries via
    ``asyncio.gather``. Entries whose envelope build returns ``None``
    (missing request_id — should not happen in practice) drop out.
    """
    from slashid_ai_forwarder_core.events import build_event_from_normalized
    from slashid_ai_forwarder_core.normalize.finalize import finalize
    from slashid_ai_forwarder_core.normalize.gemini.normalize import (
        to_normalized_invocation,
    )
    from slashid_ai_forwarder_core.normalize.normalized.types import (
        NormalizedInvocation,
    )

    from .event_envelope import vertex_envelope

    async def _prepare(entry: Entry) -> tuple[NormalizedInvocation, Entry]:
        normalized = await to_normalized_invocation(
            entry.request_body, entry.response_body, config=config
        )
        finalize(normalized, config=config)
        return normalized, entry

    prepared = await asyncio.gather(*(_prepare(e) for e in entries))

    async def _build(
        normalized: NormalizedInvocation, entry: Entry
    ) -> AIInvocationObservedV1 | None:
        envelope = vertex_envelope(entry)
        if envelope is None:
            return None
        return await build_event_from_normalized(normalized, envelope, config=config)

    built_or_none = await asyncio.gather(*(_build(n, e) for n, e in prepared))
    return [e for e in built_or_none if e is not None]
