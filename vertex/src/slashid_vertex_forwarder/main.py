"""Cloud Function 2nd gen entrypoint.

Cloud Scheduler → Pub/Sub topic → this function. The CloudEvent payload
is ignored; the entire operation is "for each source, poll → push →
commit its checkpoint". Any distinct payload would be a signal for
future variants (e.g. targeted replay), not v1.
"""

from __future__ import annotations

import json
import logging
from functools import cache

import functions_framework
from cloudevents.http import CloudEvent

from .audit_only_source import AuditOnlyEventSource
from .checkpoint_store import FirestoreCheckpointStore
from .config import load_config
from .event_source import BqEventSource, EventSource
from .handler import run_tick

log = logging.getLogger(__name__)

# Firestore document names are hardcoded — one per source under the
# customer-configurable ``firestore_checkpoint_collection``. Watermarks
# are internal state, not a public API surface; renaming them would
# be a breaking migration whether they were env-configurable or not.
_BQ_CHECKPOINT_DOC = "checkpoint"
_AUDIT_ONLY_CHECKPOINT_DOC = "checkpoint_audit_only"


@cache
def _sources() -> list[EventSource]:
    """Cached per Cloud Function container — the BigQuery, Firestore,
    and Cloud Logging clients are heavy to construct (auth, discovery)
    so we keep them warm across ticks. The Firestore client is shared
    between the two sources' checkpoint stores; they get independent
    documents so their watermarks don't collide.

    Nullary so ``@cache`` doesn't need to hash the ``Config`` (which
    holds a ``list[str] audit_observed_models`` — pydantic auto-``__hash__``
    tries to hash the raw dict and chokes on the list). ``load_config()``
    is itself cached, so pulling it inside is free.
    """
    from google.cloud import bigquery, firestore
    from google.cloud import logging as gcp_logging

    config = load_config()

    firestore_client = firestore.Client(
        project=config.gcp_project_id,
        database=config.firestore_database,
    )

    bq_source = BqEventSource(
        client=bigquery.Client(project=config.gcp_project_id),
        checkpoint_store=FirestoreCheckpointStore(
            client=firestore_client,
            collection=config.firestore_checkpoint_collection,
            document=_BQ_CHECKPOINT_DOC,
        ),
        config=config,
        project_id=config.gcp_project_id,
        dataset_id=config.bq_dataset,
        region=config.gcp_region,
        max_rows_per_tick=config.max_rows_per_tick,
        audit_buffer_seconds=config.audit_buffer_seconds,
    )
    sources: list[EventSource] = [bq_source]

    if config.audit_observed_models:
        audit_source = AuditOnlyEventSource(
            logging_client=gcp_logging.Client(
                project=config.gcp_project_id,
                _use_grpc=False,
            ),
            checkpoint_store=FirestoreCheckpointStore(
                client=firestore_client,
                collection=config.firestore_checkpoint_collection,
                document=_AUDIT_ONLY_CHECKPOINT_DOC,
            ),
            config=config,
            project_id=config.gcp_project_id,
            region=config.gcp_region,
            observed_models=config.audit_observed_models,
            max_entries_per_tick=config.max_rows_per_tick,
        )
        sources.append(audit_source)
    return sources


@functions_framework.cloud_event
def handler(cloud_event: CloudEvent) -> None:
    """Cloud Function 2nd gen entrypoint. Ignores the CloudEvent payload.

    Returns ``None`` — functions-framework's ``cloud_event`` decorator
    expects a void function. Tick counters land in CloudFunctions logs
    for observability rather than the return value.
    """
    del cloud_event
    counters = run_tick(sources=_sources(), config=load_config())
    log.info("tick complete: %s", json.dumps(counters, separators=(",", ":")))
