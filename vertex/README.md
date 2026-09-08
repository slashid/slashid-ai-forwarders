# Vertex AI forwarder

Customer-deployed GCP Cloud Function (2nd gen) that polls Vertex AI
`generateContent` request-response logging rows from BigQuery, normalizes
each into a canonical `AIInvocationObservedV1` event, and pushes to the
SlashID NHI subgraph.

Deployed via the Terraform module under `deploy/terraform/` (arriving
in a follow-up PR). The module provisions the BigQuery dataset + tables,
Cloud Scheduler + Pub/Sub trigger, Firestore checkpoint document, Secret
Manager entry for the push token, and enables Vertex request-response
logging on each configured publisher model via `setPublisherModelConfig`.

## Scope

- **Supported**: Gemini `generateContent` and `streamGenerateContent`.
- **Deferred**: `rawPredict` (Anthropic / Llama / Mistral on Vertex),
  server-side tool grounding, GCS fetch for `fileData` attachments
  (stubs only in v1), per-invocation identity correlation
  (`identity_details` ships as `{"kind": "gcp"}`).

## Known limitations

Vertex + GCP constraints that shape the v1 architecture. Not bugs —
gotchas to plan around. Extend as new ones are discovered.

- **Polling delivery, not push.** Cloud Scheduler ticks every minute
  by default (configurable via `poll_schedule`, a unix-cron string);
  BigQuery has no native row-level Pub/Sub, and Cloud Scheduler
  doesn't support sub-minute granularity. On low-usage projects most
  ticks fetch zero rows and burn Cloud Function invocations for
  nothing. Considered alternatives (Eventarc for BQ, BQ subscriptions)
  are wrong-direction or job-level only; the design POC (2026-09-04)
  confirmed no per-row push path exists.
- **No per-invocation identity.** BigQuery request-response rows carry
  no caller principal; Cloud Audit Logs carry the principal but not the
  payload. A time-based join is fragile under concurrency, so v1 emits
  `identity_details = {"kind": "gcp"}` with all fields empty. Downstream
  cannot tell "who called Vertex". Correlation is deferred to a later
  phase.
- **Per-model logging enrollment.** `setPublisherModelConfig` is scoped
  to one publisher model at a time — no project-wide "log every Vertex
  call" toggle. New models require adding to `observed_models` in the
  Terraform module and re-applying. First-time enablement takes ~10
  minutes to propagate.
- **Streaming `stop_reason` is heuristic.** Vertex's BQ log for
  `streamGenerateContent` drops the merged entry's `finishReason`
  (`null`), so a streaming event's `stop_reason` reflects a
  best-effort recovery: `max_tokens` when
  `generationConfig.maxOutputTokens` was set and the response used
  every allowed token; `end_turn` otherwise. Rare misclassifications
  are possible (a normal-completion response that happens to hit the
  token cap exactly). Client-aborted streams don't log a BQ row at
  all — the forwarder is honest about not observing them.

## Development

```bash
(cd vertex && uv run pytest)     # runs against fake BigQuery / Firestore doubles
```

Runtime deps (`functions-framework`, `google-cloud-bigquery`,
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
| `SLASHID_ENDPOINT` | yes | — |
| `SLASHID_PUSH_TOKEN` | yes | — |
| `SLASHID_GCP_PROJECT_ID` | yes | — |
| `SLASHID_GCP_REGION` | yes | — |
| `SLASHID_BQ_DATASET` | no | `slashid_vertex_reqresp_logs` |
| `SLASHID_FIRESTORE_DATABASE` | no | `slashid-vertex` |
| `SLASHID_FIRESTORE_CHECKPOINT_COLLECTION` | no | `slashid_vertex` |
| `SLASHID_FIRESTORE_CHECKPOINT_DOCUMENT` | no | `checkpoint` |
| `SLASHID_MAX_ROWS_PER_TICK` | no | `1000` |
| `SLASHID_INCLUDE_RAW_CONTENT` | no | `false` |
| `SLASHID_MAX_CONTENT_SIZE` | no | `100000` |

## Release

```bash
git tag vertex-v0.1.0
git push origin vertex-v0.1.0
```

Publishes the Cloud Function source zip + Terraform module archive to
GitHub Releases (workflow lands in a follow-up PR).
