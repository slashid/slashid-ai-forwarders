"""Vertex forwarder configuration.

Extends ``BaseConfig`` with the Vertex-specific env vars — GCP project +
region, the BigQuery dataset holding per-model request-response tables,
and the Firestore document path used as the polling checkpoint.
``load_config`` is cached so the process parses it once.
"""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import Field, model_validator
from slashid_ai_forwarder_core.config_base import BaseConfig


class Config(BaseConfig):
    """Vertex-specific forwarder runtime configuration."""

    # The cloud the backends are built on; ``main.py`` resolves it with
    # ``platforms.get``. The fields below it are that cloud's.
    platform: Literal["gcp"] = "gcp"
    project_id: str = Field(..., min_length=1)
    # The service account whose OIDC token POST /tick accepts. Unset
    # refuses every tick: this check is what guards the route.
    tick_principal: str | None = None
    # The audience that token must carry, when the deployment can name it.
    # The service's own URI is not available to the Terraform that sets
    # its environment, so it may be left unset; Cloud Run enforces the
    # audience wherever the service is not public.
    tick_audience: str | None = None
    # Regions the forwarder observes. One BigQuery source per entry,
    # plus one audit-only source whose Cloud Logging filter OR's every
    # entry. Required — the forwarder needs at least one region.
    gcp_regions: list[str] = Field(default_factory=list)
    # BigQuery dataset PREFIX. The actual per-region dataset name is
    # ``{bq_dataset_prefix}_{region_slug}`` — ``region_slug`` is
    # ``region.replace("-", "_")`` (BQ dataset IDs disallow ``-``).
    # Multiple regions get multiple datasets, each pinned to that
    # region's location; a single-region deploy is the trivial case.
    bq_dataset_prefix: str = Field(default="slashid_vertex_reqresp_logs", min_length=1)
    # Named Firestore database — multi-database Firestore is GA, so we
    # isolate the forwarder from the project's ``(default)`` database.
    database: str = Field(default="slashid-vertex", min_length=1)
    # Firestore collection under which the per-source checkpoint
    # documents live. Configurable so a customer with a pre-existing
    # ``(default)``-database convention can point us at a named
    # sub-collection. Document names within it are hardcoded in
    # ``main.py`` — one per source, so their watermarks don't collide.
    checkpoint_collection: str = Field(default="slashid_vertex", min_length=1)
    # Per-tick bounds — a tick runs inside one request, bounded by the
    # service timeout; 1000 rows/tick at ~50-100ms each stays well inside it.
    max_rows_per_tick: int = 1000
    # Buffer applied to BQ payload rows so Cloud Audit Logs have time
    # to land before we join. Default 30s covers p99+ of audit lag.
    # See identity-correlation phase design doc.
    audit_buffer_seconds: int = 30
    # Non-Google publisher/model pairs to observe via audit logs.
    # Empty list disables AuditOnlyEventSource at wiring time.
    audit_observed_models: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_regions(self) -> Config:
        """The forwarder needs at least one region to observe."""
        if not self.gcp_regions:
            raise ValueError(
                "at least one region required: set SLASHID_GCP_REGIONS "
                '(JSON list, e.g. \'["us-central1","europe-west1"]\')'
            )
        return self


@cache
def load_config() -> Config:
    """Load the forwarder config once per process."""
    return Config()  # pydantic-settings fills required fields from env
