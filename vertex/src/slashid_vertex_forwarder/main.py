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

from .checkpoint_store import FirestoreCheckpointStore
from .config import Config, load_config
from .event_source import BqEventSource, EventSource
from .handler import run_tick

log = logging.getLogger(__name__)


@cache
def _sources(config: Config) -> list[EventSource]:
    """Cached per Cloud Function container — the BigQuery and Firestore
    clients are heavy to construct (auth, discovery) so we keep them
    warm across ticks. AuditOnlyEventSource construction lands in a
    later chunk once the class exists."""
    from google.cloud import bigquery, firestore

    firestore_client = firestore.Client(
        project=config.gcp_project_id,
        database=config.firestore_database,
    )

    bq_source = BqEventSource(
        client=bigquery.Client(project=config.gcp_project_id),
        checkpoint_store=FirestoreCheckpointStore(
            client=firestore_client,
            collection=config.firestore_checkpoint_collection,
            document=config.firestore_checkpoint_document,
        ),
        config=config,
        project_id=config.gcp_project_id,
        dataset_id=config.bq_dataset,
        region=config.gcp_region,
        max_rows_per_tick=config.max_rows_per_tick,
        audit_buffer_seconds=config.audit_buffer_seconds,
    )
    sources: list[EventSource] = [bq_source]
    return sources


@functions_framework.cloud_event
def handler(cloud_event: CloudEvent) -> None:
    """Cloud Function 2nd gen entrypoint. Ignores the CloudEvent payload.

    Returns ``None`` — functions-framework's ``cloud_event`` decorator
    expects a void function. Tick counters land in CloudFunctions logs
    for observability rather than the return value.
    """
    del cloud_event
    config = load_config()
    counters = run_tick(sources=_sources(config), config=config)
    log.info("tick complete: %s", json.dumps(counters, separators=(",", ":")))
