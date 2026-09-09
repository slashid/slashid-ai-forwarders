"""Cloud Function 2nd gen entrypoint.

Cloud Scheduler → Pub/Sub topic → this function. The CloudEvent payload
is ignored; the entire operation is "load checkpoint, poll BQ, push
events, save checkpoint". Any distinct payload would be a signal for
future variants (e.g. targeted replay), not v1.
"""

from __future__ import annotations

import json
import logging
from functools import cache

import functions_framework
from cloudevents.http import CloudEvent

from .checkpoint_store import FirestoreCheckpointStore
from .config import Config, load_config
from .event_source import BqEventSource
from .handler import run_tick

log = logging.getLogger(__name__)


@cache
def _source(config: Config) -> BqEventSource:
    """Cached per Cloud Function container — the BigQuery client is
    heavy to construct (auth, discovery) so we keep one instance warm
    across ticks."""
    from google.cloud import bigquery

    return BqEventSource(
        client=bigquery.Client(project=config.gcp_project_id),
        project_id=config.gcp_project_id,
        dataset_id=config.bq_dataset,
        region=config.gcp_region,
        max_rows_per_tick=config.max_rows_per_tick,
        audit_buffer_seconds=config.audit_buffer_seconds,
    )


@cache
def _checkpoint_store(config: Config) -> FirestoreCheckpointStore:
    """Cached per Cloud Function container — Firestore client construction
    is similarly heavy."""
    from google.cloud import firestore

    return FirestoreCheckpointStore(
        client=firestore.Client(
            project=config.gcp_project_id,
            database=config.firestore_database,
        ),
        collection=config.firestore_checkpoint_collection,
        document=config.firestore_checkpoint_document,
    )


@functions_framework.cloud_event
def handler(cloud_event: CloudEvent) -> None:
    """Cloud Function 2nd gen entrypoint. Ignores the CloudEvent payload.

    Returns ``None`` — functions-framework's ``cloud_event`` decorator
    expects a void function. Tick counters land in CloudFunctions logs
    for observability rather than the return value.
    """
    del cloud_event
    config = load_config()
    counters = run_tick(
        source=_source(config),
        checkpoint_store=_checkpoint_store(config),
        config=config,
    )
    log.info("tick complete: %s", json.dumps(counters, separators=(",", ":")))
