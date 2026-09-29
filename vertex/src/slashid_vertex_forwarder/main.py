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
from slashid_ai_forwarder_core.platform.gcp import GcpPlatform

from .audit_only_source import AuditOnlyEventSource
from .config import load_config
from .event_source import BqEventSource, EventSource
from .handler import run_tick

log = logging.getLogger(__name__)

# Firestore document names are hardcoded — one per source under the
# customer-configurable ``firestore_checkpoint_collection``. Watermarks
# are internal state, not a public API surface; renaming them would
# be a breaking migration whether they were env-configurable or not.
# BQ path has a checkpoint per region (its BQ dataset is regional);
# the audit path is cross-region by construction (one Cloud Logging
# query over the OR-clause of ``config.gcp_regions``) so a single
# global doc covers it.
_AUDIT_ONLY_CHECKPOINT_DOC = "checkpoint_audit_only"


def _bq_checkpoint_doc(region: str) -> str:
    """Per-region BQ checkpoint doc name. ``region`` maps to a BQ dataset
    whose watermark is independent from every other region's."""
    slug = region.replace("-", "_")
    return f"checkpoint_bq_{slug}"


def _bq_dataset_id(prefix: str, region: str) -> str:
    """Per-region dataset name — BQ dataset IDs can't contain ``-``,
    so replace with ``_``. Matches the naming used by the Terraform
    module (``for_each`` on ``google_bigquery_dataset.reqresp``)."""
    slug = region.replace("-", "_")
    return f"{prefix}_{slug}"


@cache
def _sources() -> list[EventSource]:
    """Cached per Cloud Function container — the BigQuery, Firestore,
    and Cloud Logging clients are heavy to construct (auth, discovery)
    so we keep them warm across ticks. The Firestore client is shared
    across every source's checkpoint store; each source gets its own
    document so watermarks don't collide.

    Multi-region: one ``BqEventSource`` per region (each with its own
    regional dataset + checkpoint doc) plus a single
    ``AuditOnlyEventSource`` whose Cloud Logging filter OR's every
    ``gcp_regions`` entry — audit logs are globally aggregated so a
    single query covers all regions.

    Nullary so ``@cache`` doesn't need to hash the ``Config`` (which
    holds ``list[str]`` fields — pydantic auto-``__hash__`` tries to
    hash the raw dict and chokes on lists). ``load_config()`` is
    itself cached, so pulling it inside is free.
    """
    from google.cloud import bigquery
    from google.cloud import logging as gcp_logging

    config = load_config()

    platform = GcpPlatform(project=config.gcp_project_id, database=config.firestore_database)
    bq_client = bigquery.Client(project=config.gcp_project_id)

    sources: list[EventSource] = [
        BqEventSource(
            client=bq_client,
            checkpoint_store=platform.checkpoint_store(
                collection=config.firestore_checkpoint_collection,
                document=_bq_checkpoint_doc(region),
            ),
            config=config,
            project_id=config.gcp_project_id,
            dataset_id=_bq_dataset_id(config.bq_dataset_prefix, region),
            region=region,
            max_rows_per_tick=config.max_rows_per_tick,
            audit_buffer_seconds=config.audit_buffer_seconds,
        )
        for region in config.gcp_regions
    ]

    if config.audit_observed_models:
        audit_source = AuditOnlyEventSource(
            logging_client=gcp_logging.Client(
                project=config.gcp_project_id,
                _use_grpc=False,
            ),
            checkpoint_store=platform.checkpoint_store(
                collection=config.firestore_checkpoint_collection,
                document=_AUDIT_ONLY_CHECKPOINT_DOC,
            ),
            config=config,
            project_id=config.gcp_project_id,
            regions=config.gcp_regions,
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
