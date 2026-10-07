# Vertex AI forwarder

Customer-deployed GCP Cloud Run service that polls Vertex AI
`generateContent` request-response logging rows from BigQuery, normalizes
each into a canonical `AIInvocationObservedV1` event, and pushes to the
SlashID NHI subgraph.

Deployed via the Terraform module under `deploy/terraform/` (arriving
in a follow-up PR). The module provisions the BigQuery dataset + tables,
the Cloud Run service and its Cloud Scheduler job, Firestore checkpoint
document, Secret Manager entry for the push token, and enables Vertex request-response
logging on each configured publisher model via `setPublisherModelConfig`.

## Scope

- **Supported**: Gemini `generateContent` and `streamGenerateContent`.
- **Deferred**: `rawPredict` (Anthropic / Llama / Mistral on Vertex),
  server-side tool grounding.

## Known limitations

Vertex + GCP constraints that shape the v1 architecture. Not bugs —
gotchas to plan around. Extend as new ones are discovered.

- **Request-response logging validates its table asynchronously.**
  `setPublisherModelConfig` answers with an operation, and Vertex checks
  the destination table's schema inside it. A column it doesn't expect as
  `REQUIRED` (it wants `logging_time` and `request_id` NULLABLE) fails the
  operation, so logging silently stays off. The module waits for each
  operation and fails the apply on an error.

- **Polling delivery, not push.** Cloud Scheduler ticks every minute
  by default (configurable via `poll_schedule`, a unix-cron string);
  BigQuery has no native row-level Pub/Sub, and Cloud Scheduler
  doesn't support sub-minute granularity. On low-usage projects most
  ticks fetch zero rows and burn Cloud Run requests for nothing. Considered alternatives (Eventarc for BQ, BQ subscriptions)
  are wrong-direction or job-level only; the design POC (2026-09-04)
  confirmed no per-row push path exists.
- **Identity resolution is buffered.** Payload events are held for 30s
  (configurable via `audit_buffer_seconds`) so Cloud Audit Logs have
  time to land for the identity-correlation join. Total observed
  latency from Gemini call to emitted event is ~90-120s (30s
  forwarder buffer + 60s BQ streaming buffer). Lower the buffer to
  trade identity coverage for lower latency.
- **Multi-tenant ambiguity yields partial identity.** When two
  distinct callers hit the same Gemini model + method within the
  correlation window (200-300ms depending on the bucket),
  per-field per-position consensus emits only the fields where every
  candidate agrees. Same user via two OAuth clients →
  `credential_chain[0].principal_email` still emitted, `oauth_client_id`
  drops. Two distinct users → identity drops entirely
  (`credential_chain = None`). Rare for single-tenant projects;
  possible in high-QPS multi-tenant deployments.
- **`locations/global` has no data residency.** Observing `global`
  (opt-in via the `regions` Terraform variable) captures Console and
  Vertex AI Studio traffic, but Google routes each request to whichever
  region has capacity and never discloses which. The payload lands in
  the BigQuery dataset you chose; where it was *processed* is not
  knowable. BigQuery also has no `global` location, so that dataset is
  created in the deployment region rather than alongside the
  inference — and because dataset location is immutable, reordering
  `regions` so the deployment region changes destroys and recreates it,
  losing unprocessed rows.
- **Per-model logging enrollment.** `setPublisherModelConfig` is scoped
  to one publisher model at a time — no project-wide "log every Vertex
  call" toggle. New models require adding to `observed_models` in the
  Terraform module and re-applying. First-time enablement takes ~10
  minutes to propagate.
- **Non-Google publisher observability is audit-log-only.** Vertex's
  BQ request-response logging (`setPublisherModelConfig`) is
  Google-only — verified empirically. Non-Google publishers
  (Anthropic, Meta, Mistral AI, xAI, …) surface as sparse
  `AIInvocationObservedV1` events with `parsed_as="vertex-audit"`:
  identity, timestamp, and model reference are populated; tokens,
  stop reason, and input/output payloads are null. See Phase 3.7
  design doc for the mechanism.
- **Errored Google calls take the audit path too.** Vertex's payload
  BQ logging is response-conditional — errored `generateContent` /
  `rawPredict` calls never land in BQ. The audit-only source's
  server-side filter is
  `NOT publishers/google/ OR protoPayload.status.code!=0`, so
  errored Google entries flow through the same sparse-event pipeline
  as non-Google traffic with `stop_reason="error"`. Successful
  Google calls stay on the BQ payload path; no double-count.
- **OpenAI-compat endpoint traffic is not observable.** Calls to
  Vertex's `/endpoints/openapi/chat/completions` (and its
  `completions` / `embeddings` siblings) do produce Cloud Audit Log
  entries, but the request body is opaque `HttpBody` — the model
  isn't captured anywhere in the log, and there's no downstream
  audit trail for the resolved publisher/model either. The entries
  carry identity and timestamp but not the *what*, and the forwarder
  emits nothing for them. This is the default way to call most
  model-as-a-service models (gpt-oss, Llama, DeepSeek, Qwen), so that
  traffic goes unrecorded. Route via
  `/publishers/{publisher}/models/{model}:rawPredict` where the
  publisher supports it if you need model-attributed observability.
- **Streaming `stop_reason` is heuristic.** Vertex's BQ log for
  `streamGenerateContent` drops the merged entry's `finishReason`
  (`null`), so a streaming event's `stop_reason` reflects a
  best-effort recovery: `max_tokens` when
  `generationConfig.maxOutputTokens` was set and the response used
  every allowed token; `end_turn` otherwise. Rare misclassifications
  are possible (a normal-completion response that happens to hit the
  token cap exactly). Client-aborted streams don't log a BQ row at
  all — the forwarder is honest about not observing them.
- **`fileData` requires bucket IAM grants.** Attachments referenced by
  `gs://` URI need `roles/storage.objectViewer` on the containing
  bucket for the forwarder SA. Grant per-bucket via
  `filedata_buckets = ["bucket-a", ...]` in the Terraform module, or
  project-wide via `filedata_buckets = ["*"]`. Unlisted or unreadable
  buckets emit stub `AIAccessedFile` entries (URI + media_type, no
  hash, no byte_length) instead of failing the tick.
- **Cross-project `fileData` buckets.** The TF module grants IAM only
  against `project_id`; buckets in other GCP projects need
  customer-managed IAM. The forwarder still stubs them if
  inaccessible.

## Development

```bash
(cd vertex && uv run pytest)     # runs against fake BigQuery / Firestore doubles
```

Runtime deps (`fastapi`, `uvicorn`, `google-cloud-bigquery`,
`google-cloud-firestore`, `google-cloud-secret-manager`) are declared in
`pyproject.toml`; the workspace-level `uv sync` installs them.

Live smoke against a real GCP project:

```bash
(cd vertex && ./generate-content <MODEL> <PROMPT>)   # coming soon
```

## Configuration

All env vars use the `SLASHID_` prefix:

| var | required | default |
| --- | --- | --- |
| `SLASHID_ENDPOINT` | no | `https://api.slashid.com` |
| `SLASHID_PUSH_TOKEN` | yes | — |
| `SLASHID_PROJECT_ID` | yes | — |
| `SLASHID_GCP_REGIONS` | yes | — (JSON list, e.g. `["us-central1","europe-west1"]`) |
| `SLASHID_TICK_PRINCIPAL` | yes, to accept ticks | — (the service account whose OIDC token `POST /tick` accepts; unset refuses every tick) |
| `SLASHID_TICK_AUDIENCE` | no | — (checked only when set) |
| `SLASHID_BQ_DATASET_PREFIX` | no | `slashid_vertex_reqresp_logs` (per-region dataset name = `{prefix}_{region_slug}`) |
| `SLASHID_DATABASE` | no | `slashid-vertex` |
| `SLASHID_CHECKPOINT_COLLECTION` | no | `slashid_vertex` |
| `SLASHID_FIRESTORE_CHECKPOINT_DOCUMENT` | no | `checkpoint` |
| `SLASHID_MAX_ROWS_PER_TICK` | no | `1000` |
| `SLASHID_INCLUDE_RAW_CONTENT` | no | `false` |
| `SLASHID_MAX_CONTENT_SIZE` | no | `100000` |
| `SLASHID_INPUT_SCOPE` | no | `round` (the messages since the last response; `session` sends the whole transcript) |
| `SLASHID_ROUND_LINK_DEPTH` | no | `10` (rounds listed in `recent_round_hashes`) |

## Release

```bash
git tag vertex-v0.1.0
git push origin vertex-v0.1.0
```

Builds the container image and pushes it to
`ghcr.io/slashid/slashid-ai-forwarder-vertex:<version>`, and publishes the
GitHub Release naming the image and the Terraform module `ref`.
