# Vertex on Cloud Run, async end to end

**Date:** 2026-10-01
**Status:** Design, ready for planning.
**Target repo:** `slashid-ai-forwarders`: `shared/`, `anthropic/`, `vertex/` (code, `deploy/terraform`, `Dockerfile`) and the Vertex release workflow.
**Follows:** the platform seam (#69). It includes the "async checkpoint store" follow-up deferred there.

## Problem

Vertex is a Pub/Sub-triggered Cloud Function, and the only forwarder shaped that way. Anthropic is an async FastAPI service on Cloud Run with a scheduler-driven `/tick`. The difference costs us:

- A functions-framework handler is sync and has no loop of its own, so each invocation calls `asyncio.run`. Anything loop-bound, such as an async Firestore client, cannot live across ticks, and the code carries nested `asyncio.run` islands to reach async code from sync (`event_source.py`, `handler.py`).
- `CheckpointStore.load/save` are sync, so `GcpPlatform` holds two Firestore clients (one only for checkpoints), and Anthropic's `FeedCursor` blocks its event loop on them.
- The function ships as a zip with its own machinery: a release bucket, a `gh release download` provisioner, an md5 cache that goes stale on re-tag, and a Cloud Functions build layer that mis-resolves dependencies.
- Concurrency=1 comes from a Pub/Sub setting instead of the tick lease the seam already provides.

## Goals

1. Vertex runs as a FastAPI service on Cloud Run, driven by Cloud Scheduler over HTTP with an OIDC token, on the same seam as Anthropic (`scheduler_auth`, `tick_lease`).
2. Async end to end: `CheckpointStore` is async, `GcpPlatform` has one (async) Firestore client, Vertex's `EventSource.fetch/commit` are async, Anthropic's `FeedCursor` awaits.
3. Vertex ships as a container image, as Anthropic does.

## Non-goals

What Vertex collects and how it builds events is unchanged. The BigQuery datasets, `setPublisherModelConfig`, Firestore and the push-token secret in Terraform are unchanged. No SQLite platform (next). No zero-gap cutover and no deprecation path: nothing has shipped to customers, so variables and outputs can be renamed or removed outright. The version stays 0.1.x.

## Design

### Async checkpoints (`shared/`, `anthropic/`)

`platform/checkpoint.py`: `load` and `save` become `async def`. `platform/gcp/firestore.py`: `FirestoreCheckpointStore` takes the async client and awaits `get` and `set`; the `Client` comments become `AsyncClient`. `GcpPlatform.firestore` becomes the cached `AsyncClient` and `firestore_async` is removed; `checkpoint_store`, `tick_lease` and Anthropic's `platform.py` all use `firestore`.

Anthropic's `FeedCursor.window_start`, `advance` and `window_age_seconds` become `async`. Call sites: `compliance/denials.py:73` and `:99`; `compliance/responses.py:277` (inside the `drain_local_sessions(since=…)` argument, under `asyncio.timeout`, so the read now counts against that budget), `:317-318` (two `advance` calls) and `:453` (the argument of an `async for` header). Each `window_start` is hoisted to a `since = await …` line. `advance(drained=False)` already loads twice (its own `load` and `window_age_seconds`); kept as is.

### Vertex service (`vertex/src`)

`main.py` becomes the uvicorn factory (`slashid_vertex_forwarder.main:app --factory`) with `create_app(config, *, sources, lease, tick_auth)`, every stateful piece injected so tests hand in fakes. One route, `POST /tick`:

1. The bearer token is checked with the platform's `scheduler_auth(principal, audience)`; a missing or refused token is `401`, and an unset principal refuses everything (fail closed).
2. The tick runs inside `lease.hold(TICK_LEASE)`, a constant of the Vertex app (ten minutes, longer than the service timeout). When the lease is not held the answer is `200 {"skipped": true}`; it is not an error.
3. Otherwise `run_tick` runs and the counters it logs today (`events_pushed`, `envelopes_seen`) are the JSON body.

`Config` gains `platform`, `tick_principal` and `tick_audience`, the same fields Anthropic has, and the sources are built through `platforms.get("gcp", …)` rather than `GcpPlatform(...)` directly. The lease uses the checkpoint collection, document `tick`. The module-level `log.setLevel` in `handler.py` goes: uvicorn configures only its own loggers, so `app()` calls `logging.basicConfig(level=LOG_LEVEL, stream=sys.stderr)` as Anthropic's does, and `INFO` lines such as `tick complete:` reach Cloud Logging.

`run_tick` becomes `async def`. The per-source `try/except` is unchanged, so one source failing does not stop the next. `EventSource.fetch` and `commit` become `async def` on the protocol, `BqEventSource` and `AuditOnlyEventSource`. Their blocking halves run in a worker thread: in `BqEventSource` the BigQuery query and row loop (a sync helper that returns the parsed rows, the raw count and the max `(timestamp, id)`) and `_query_audit`; in `AuditOnlyEventSource`, `query_audit_only_entries`. Identity stamping, envelope building and checkpoint arithmetic stay sync and inline. The `asyncio.run` islands become `await _gemini_pipeline(...)` and `await _push_events(...)`. `commit` awaits the checkpoint save.

There is one loop for the life of the process, so the BigQuery, Cloud Logging and Firestore clients are built once and cached; the per-tick workaround for loop affinity is not needed. That includes the Logging client `BqEventSource._query_audit` builds on every call today: it is created once in the source. The sync clients are used from one thread at a time: a source awaits each helper before starting the next, and sources run one after another. The whole tick runs inside the request, so the service keeps request-based CPU and does not copy Anthropic's `cpu_idle = false`, which exists for work after the response.

### Build and release

`vertex/Dockerfile` is a twin of `anthropic/Dockerfile` for the `slashid-vertex-forwarder` package: the same four workspace manifest copies (`shared`, `bedrock`, `vertex`, `anthropic`), then `shared/src` and `vertex/src`. `pyproject.toml` drops `functions-framework` and adds `fastapi` and `uvicorn`, and `uv.lock` is regenerated in the same change, since the image builds with `uv sync --frozen`.

`release-vertex.yml` mirrors `release-anthropic.yml`: `packages: write`, buildx, login and `build-push-action` with `file: vertex/Dockerfile`, pushing `ghcr.io/slashid/slashid-vertex-forwarder:<version>` (the bare version, `vertex-v` stripped, which is what the module's image local derives). It keeps the tag-versus-`pyproject.toml` check and the GitHub release, whose body names the image and the module `ref=`. The zip, `setup-uv` and the staging steps go. A new GHCR package is private, so the first deploy needs `ghcr_username` and `ghcr_token`, as for Anthropic.

### Terraform (`vertex/deploy/terraform`)

- **Added:** a `google_cloud_run_v2_service` named `service_name` (default `slashid-vertex-forwarder`), internal ingress, `deletion_protection = false`, `max_instance_count` default 1, request concurrency 20 (so an overlapping tick is answered at once by the lease rather than queued), the tick timeout, the runtime service account, and a `depends_on` mirroring Anthropic's (the push-token secret version, the registry, and the runtime IAM grants, since a `latest` secret reference creates no dependency by itself). A scheduler service account with `run.invoker` on that service. A ghcr remote Artifact Registry repository as in the Anthropic module, with its `data.google_project`, the `ghcr_token` secret and version (write-once, #75), the registry service agent's secret read, and optionally `artifactregistry.reader` for the runtime account. APIs `artifactregistry`, `cloudresourcemanager` and `iam`.
- **Changed:** the scheduler job becomes an `http_target` `POST /tick` with an OIDC token for that service account (audience: the service URI), `retry_count = 0`, and `attempt_deadline` set from a new `tick_attempt_deadline_seconds` (default 540; the HTTP default is 180 and would give up on a tick Cloud Run is still running), with the same precondition as Anthropic that it not exceed `service_timeout_seconds`. The service's environment sets `SLASHID_TICK_PRINCIPAL` to the scheduler account and leaves the audience unset: it is the service's own URI, and putting it in that service's environment is a cycle.
- **Removed:** the Cloud Function, the Pub/Sub topic and the `pubsub_subscriber` grant, the project-wide `run_invoker` grant, the release bucket, `download_release_zip`, the `local.release_*` locals, the `archive` provider if nothing uses it, and the `cloudfunctions`, `eventarc`, `pubsub` and `cloudbuild` APIs after confirming nothing that stays references them (`storage` stays: the `fileData` bucket grants use it). Variables `release_repo`, `release_bucket_name`, `function_name` and `trigger_topic_name` are removed.
- **Renamed or added:** `service_name`, `image` and `ghcr_*`, instance, memory and timeout knobs named as in the Anthropic module, and the `function_uri` output becomes `service_uri` (the `trigger_topic` output goes). `release_version` stays and now selects the image tag.
- **Unchanged:** BigQuery, the publisher-model logging config, Firestore, the secret, `poll_schedule`.
- Comments that name the function (`iam.tf`, `secrets.tf`, `main.tf`, `variables.tf`, `versions.tf`) and the README (the `gh` and zip prerequisites, the API list, the naming section) are rewritten.

### Moving the existing deployment

A 2nd-gen function is itself a Cloud Run service with the function's name, which is also the new service's name, so one apply would race to create a service the function still owns. The existing deployment therefore moves in two steps: a targeted apply that destroys `google_cloudfunctions2_function.forwarder` (it is in state and no longer in config), then a full apply that creates the service, switches the scheduler job and destroys the topic and release bucket. Polling pauses between the two; BigQuery logs persist, the Firestore checkpoints resume where they were, and the server dedups on `request_id`, so nothing is lost. A plan against the live state before each step must list exactly those changes, show the scheduler job as an in-place update rather than a replacement, and touch nothing in BigQuery, Firestore or the secret. Nothing has shipped to customers, so this is a note for the one existing deployment and not a README section.

## Testing

- Shared: `test_checkpoint.py` (`_FakeDocRef`) and `test_platform_gcp.py` move to async fakes; assert `GcpPlatform` exposes one client, `firestore`, and no `firestore_async`.
- Anthropic: `tests/test_cursors.py` `_FakeStore` becomes async (it is imported as `FakeCheckpoints` by `test_responses.py`, `test_denials.py`, `test_response_tail.py`, `test_readers.py`, which then await cursors). Window arithmetic assertions are unchanged.
- Vertex: the sync `def test_*` functions that call `fetch`, `commit` or `run_tick` (about 40 call sites across `test_bq_event_source.py`, `test_audit_only_event_source.py`, `test_handler.py`) become `async def`; `_FakeCheckpointStore` (two files) and `_FakeSource` become async. Tests that monkeypatch `_query_audit` keep doing so (the source reaches it through `self._query_audit`).
- `test_main.py` is rewritten around the app factory with `ASGITransport` (as Anthropic's tests do), and its `@cache`-clearing test goes with the cache: `/tick` refused with no token and with an unset principal, lease not held returns `skipped`, counters returned, one source raising does not stop the next, and a failure in a thread helper reaches the per-source `try/except` with the checkpoint not committed.
- `terraform fmt` and `validate` (already in CI), and the live plan described above.
- `asyncio_mode = "auto"` is already set in every package.

## Rollout and verification

After the merge and a release, move the live deployment in the two steps above, then trigger one tick (`gcloud scheduler jobs run`) and check the `tick complete:` log line, that the checkpoint documents advance, and that no `401` or lease errors appear. Then run a Gemini conversation and check events and stitching as in the earlier validation.

## Risks

- A forgotten `await` returns a coroutine that never runs and a checkpoint that silently does not move. The type checker flags an unawaited coroutine used as a value, and the tests assert the stored checkpoint after `commit`.
- The scheduler token is the only guard on `/tick` if ingress were ever opened; the service keeps internal ingress and `scheduler_auth` fails closed.
- Pulling from the ghcr proxy needs `ghcr_username` and `ghcr_token` while the repository is private, as for Anthropic.
- Exceptions from the thread helpers propagate through the `await` into the existing per-source `try/except`, which catches `Exception`; the failure test above pins that.

## Stale text to fix in the same change

`event_source.py` ("a single `asyncio.run` inside fetch"), `handler.py` and `main.py` module docstrings (Cloud Function, Pub/Sub, concurrency=1), `config.py` ("warm Cloud Function instances", "9-minute max runtime"), `platform/gcp/firestore.py` (`Client`), the `GcpPlatform` docstring ("synchronous client"), the Vertex README, and the Terraform file headers that describe the function.
