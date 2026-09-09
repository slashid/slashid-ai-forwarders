"""Vertex forwarder configuration.

Extends ``BaseConfig`` with the Vertex-specific env vars — GCP project +
region, the BigQuery dataset holding per-model request-response tables,
and the Firestore document path used as the polling checkpoint.
``load_config`` is cached so warm Cloud Function instances reuse the
parsed instance across invocations.
"""

from __future__ import annotations

from functools import cache

from pydantic import Field
from slashid_ai_forwarder_core.config_base import BaseConfig


class Config(BaseConfig):
    """Vertex-specific forwarder runtime configuration."""

    gcp_project_id: str = Field(..., min_length=1)
    gcp_region: str = Field(..., min_length=1)
    # BigQuery dataset that holds one table per logged publisher model
    # (see Terraform module — ``slashid_vertex_reqresp_<model_slug>``).
    bq_dataset: str = Field(default="slashid_vertex_reqresp_logs", min_length=1)
    # Named Firestore database — multi-database Firestore is GA, so we
    # isolate the forwarder from the project's ``(default)`` database.
    firestore_database: str = Field(default="slashid-vertex", min_length=1)
    # Firestore collection/document path used as the polling checkpoint.
    # Value defaults match the Terraform module's provisioned names.
    firestore_checkpoint_collection: str = Field(default="slashid_vertex", min_length=1)
    firestore_checkpoint_document: str = Field(default="checkpoint", min_length=1)
    # Per-tick bounds — Cloud Function 2nd gen has a 9-minute max runtime;
    # 1000 rows/tick at ~50-100ms each stays well inside that.
    max_rows_per_tick: int = 1000
    # Buffer applied to BQ payload rows so Cloud Audit Logs have time
    # to land before we join. Default 30s covers p99+ of audit lag.
    # See identity-correlation phase design doc.
    audit_buffer_seconds: int = 30


@cache
def load_config() -> Config:
    """Load the forwarder config once per Cloud Function container."""
    return Config()  # pydantic-settings fills required fields from env
