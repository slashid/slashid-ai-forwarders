"""Polled ``EventSource`` protocol + BigQuery-backed implementation.

BigQuery request-response logging (enabled per publisher model via
``setPublisherModelConfig``) writes each Vertex ``generateContent`` call
to a per-model table with the shape documented at
https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/request-response-logging.
This module polls those tables on each Cloud Scheduler tick.

The abstraction (``EventSource`` protocol + ``Entry`` dataclass) keeps
the handler loop source-agnostic — a later phase can swap in a joined
BQ view or a Pub/Sub push subscription behind the same interface.

Checkpoint format: ``(last_logging_time, last_request_id)``. The BQ
query filters ``logging_time > last_logging_time`` OR (equal AND
``request_id > last_request_id``). Boundary collisions are safe — the
server dedupes on ``request_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiRequestBody,
    GeminiResponse,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Checkpoint:
    """The polling watermark — ``(logging_time, request_id)`` of the
    last processed row across every logged model.

    ``last_logging_time = None`` means "no rows seen yet"; fetch pulls
    every row up to the batch bound.
    """

    last_logging_time: datetime | None
    last_request_id: str | None


@dataclass(frozen=True)
class Entry:
    """One row from a BQ request-response logging table.

    ``request_body`` / ``response_body`` are typed pydantic — the source
    validates raw JSON at fetch time so the handler pipeline gets
    canonical inputs. ``model_path`` is the full publisher model path
    (``publishers/google/models/gemini-2.5-flash``) — used as-is for
    ``AIModel.id``.
    """

    request_id: str
    logging_time: datetime
    model_path: str
    region: str
    request_body: GeminiRequestBody
    response_body: GeminiResponse

    @property
    def checkpoint(self) -> Checkpoint:
        """Checkpoint pointing at this entry — save after successful push."""
        return Checkpoint(
            last_logging_time=self.logging_time,
            last_request_id=self.request_id,
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
    ) -> None:
        self._client = client
        self._project_id = project_id
        self._dataset_id = dataset_id
        self._region = region
        self._max_rows_per_tick = max_rows_per_tick

    def fetch(self, checkpoint: Checkpoint) -> list[Entry]:
        query, params = self._build_query(checkpoint)
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(query_parameters=params)
        job = self._client.query(query, job_config=job_config)
        entries: list[Entry] = []
        for row in job.result():
            entry = _row_to_entry(row, region=self._region)
            if entry is not None:
                entries.append(entry)
        return entries

    def _build_query(
        self,
        checkpoint: Checkpoint,
    ) -> tuple[str, list[Any]]:
        """Return the parameterised SQL + query-parameters for one tick.

        Reads every ``slashid_vertex_reqresp_*`` table in the dataset via
        a wildcard table reference. Filter on ``logging_time`` +
        ``request_id`` ordering to progress past the checkpoint on every
        tick.
        """
        from google.cloud import bigquery

        # Wildcard table read — one FROM covers every provisioned model
        # table without the handler having to know the model list.
        table_glob = f"`{self._project_id}.{self._dataset_id}.slashid_vertex_reqresp_*`"
        params: list[Any] = [
            bigquery.ScalarQueryParameter("limit", "INT64", self._max_rows_per_tick),
        ]
        where = ""
        if checkpoint.last_logging_time is not None and checkpoint.last_request_id is not None:
            where = (
                "WHERE logging_time > @last_time "
                "OR (logging_time = @last_time AND CAST(request_id AS STRING) > @last_req)"
            )
            params.extend(
                [
                    bigquery.ScalarQueryParameter(
                        "last_time", "TIMESTAMP", checkpoint.last_logging_time
                    ),
                    bigquery.ScalarQueryParameter("last_req", "STRING", checkpoint.last_request_id),
                ]
            )
        query = (
            "SELECT request_id, logging_time, model, full_request, full_response "
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

    return Entry(
        request_id=str(request_id),
        logging_time=logging_time,
        model_path=str(model),
        region=region,
        request_body=request_body,
        response_body=response_body,
    )
