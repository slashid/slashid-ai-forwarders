"""FastAPI entrypoint: the scheduler's tick on POST /tick.

Cloud Scheduler posts here with an OIDC token; the tick lease makes an
overlapping tick a no-op. Run with
``uvicorn slashid_vertex_forwarder.main:app --factory``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from slashid_ai_forwarder_core import platform as platforms
from slashid_ai_forwarder_core.platform import Platform, SchedulerAuth, TickLease

from .audit_only_source import AuditOnlyEventSource
from .config import Config, load_config
from .event_source import BqEventSource, EventSource
from .handler import run_tick

log = logging.getLogger(__name__)

# Longer than the service timeout, so a crashed tick's lease lapses on its own.
TICK_LEASE = timedelta(minutes=10)

# Firestore document names are hardcoded — one per source under the
# customer-configurable ``checkpoint_collection``. Watermarks
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


def _sources(config: Config, platform: Platform) -> list[EventSource]:
    """One ``BqEventSource`` per region plus, when models are observed via
    audit logs, a single ``AuditOnlyEventSource`` whose Cloud Logging
    filter OR's every region (audit logs are globally aggregated, so one
    query covers all of them).

    The BigQuery and Cloud Logging clients are built once and live as long
    as the process; each source gets its own checkpoint document so
    watermarks don't collide.
    """
    from google.cloud import bigquery
    from google.cloud import logging as gcp_logging

    bq_client = bigquery.Client(project=config.project_id)

    sources: list[EventSource] = [
        BqEventSource(
            client=bq_client,
            checkpoint_store=platform.checkpoint_store(
                collection=config.checkpoint_collection,
                document=_bq_checkpoint_doc(region),
            ),
            config=config,
            project_id=config.project_id,
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
                project=config.project_id,
                _use_grpc=False,
            ),
            checkpoint_store=platform.checkpoint_store(
                collection=config.checkpoint_collection,
                document=_AUDIT_ONLY_CHECKPOINT_DOC,
            ),
            config=config,
            project_id=config.project_id,
            regions=config.gcp_regions,
            observed_models=config.audit_observed_models,
            max_entries_per_tick=config.max_rows_per_tick,
        )
        sources.append(audit_source)
    return sources


async def _refuse(_token: str) -> bool:
    log.error("no scheduler authentication is configured; refusing every tick")
    return False


def _bearer(header: str | None) -> str | None:
    scheme, _, token = (header or "").partition(" ")
    return token if scheme.lower() == "bearer" and token else None


@dataclass(frozen=True)
class Backends:
    sources: Sequence[EventSource]
    lease: TickLease | None
    tick_auth: SchedulerAuth | None


@contextlib.asynccontextmanager
async def open_backends(config: Config) -> AsyncIterator[Backends]:
    """The backends for the configured platform, which stays open while the
    block does. The app's lifespan holds it for the life of the process."""
    async with platforms.get(
        config.platform, project=config.project_id, firestore_database=config.database
    ) as platform:
        yield Backends(
            sources=_sources(config, platform),
            lease=platform.tick_lease(collection=config.checkpoint_collection, document="tick"),
            tick_auth=platform.scheduler_auth(
                principal=config.tick_principal, audience=config.tick_audience
            ),
        )


def create_app(
    config: Config,
    *,
    sources: Sequence[EventSource] = (),
    lease: TickLease | None = None,
    tick_auth: SchedulerAuth | None = None,
    backends: Callable[[], contextlib.AbstractAsyncContextManager[Backends]] | None = None,
) -> FastAPI:
    """Every stateful piece is injected, or opened at startup by ``backends``
    (which ``app()`` points at the configured platform) and closed at
    shutdown; tests hand in fakes."""
    authorize = tick_auth or _refuse

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The route reads these names when a request arrives, so rebinding
        # them here is all the opened backends need.
        nonlocal sources, lease, authorize
        async with contextlib.AsyncExitStack() as stack:
            if backends is not None:
                opened = await stack.enter_async_context(backends())
                sources, lease = opened.sources, opened.lease
                authorize = opened.tick_auth or _refuse
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.post("/tick")
    async def tick(request: Request) -> Response:
        token = _bearer(request.headers.get("authorization"))
        if token is None or not await authorize(token):
            log.warning("tick refused: no acceptable scheduler token")
            return Response(status_code=401)
        guard = lease.hold(TICK_LEASE) if lease is not None else contextlib.nullcontext(True)
        async with guard as held:
            if not held:
                # Not an error: the next scheduled tick picks the work up.
                return JSONResponse({"skipped": True})
            counters = await run_tick(sources=sources, config=config)
        log.info("tick complete: %s", json.dumps(counters, separators=(",", ":")))
        return JSONResponse(counters)

    return app


def app() -> FastAPI:
    """uvicorn factory: ``uvicorn slashid_vertex_forwarder.main:app --factory``.

    uvicorn configures only its own loggers, so without a root handler our
    INFO lines fall to Python's last-resort handler and are dropped.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    return create_app(config, backends=lambda: open_backends(config))
