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

- **Supported**: Gemini `generateContent` (non-streaming).
- **Deferred**: `streamGenerateContent`, `rawPredict` (Anthropic /
  Llama / Mistral on Vertex), server-side tool grounding, GCS fetch for
  `fileData` attachments (stubs only in v1), per-invocation identity
  correlation (`identity_details` ships as `{"kind": "gcp"}`).

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
