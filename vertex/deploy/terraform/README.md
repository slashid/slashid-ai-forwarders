# SlashID Vertex forwarder — Terraform module

Provisions the customer-side GCP resources for the Vertex AI forwarder
Cloud Function:

- BigQuery dataset + one table per logged publisher model.
- `setPublisherModelConfig` on each model so Vertex routes request-
  response logs into the tables.
- Firestore Native database (optional — reuse an existing one by
  setting `create_firestore_database = false`).
- Cloud Function 2nd gen (source zip fetched from GitHub Releases).
- Cloud Scheduler cron → Pub/Sub topic → Cloud Function trigger.
- Secret Manager entry for the SlashID push token.
- Service account with least-privilege role grants.
- Log-sink exclusion for Vertex Data Access audit logs (cost mitigation
  — the underlying capture stays enabled for future correlation work).

## Usage

```hcl
module "slashid_vertex_forwarder" {
  source = "github.com/slashid/slashid-ai-forwarders//vertex/deploy/terraform?ref=vertex-v0.1.0"

  project_id              = "customer-project-123456"
  region                  = "us-central1"
  logged_publisher_models = ["google/gemini-2.5-flash", "google/gemini-2.5-pro"]
  slashid_endpoint        = "https://api.slashid.com"
  slashid_push_token      = var.slashid_push_token  # sensitive

  release_version = "vertex-v0.1.0"

  # Optional — flip to true to forward prompt/response bodies alongside
  # the hash / mime / byte_length metadata.
  include_raw_content = false

  # Optional — reuse an existing Firestore Native database in this
  # project instead of creating a new one.
  create_firestore_database = true
}
```

`slashid_push_token` is sensitive — declare it as a sensitive variable
in your root module and source it from a secret manager (not `.tfvars`
committed to VCS).

## First deployment

`setPublisherModelConfig` propagation takes ~10 min for a first-time
enablement — the first BigQuery row (and therefore the first forwarded
event) may take that long to appear after `terraform apply` returns.

The module fetches the source zip from a GitHub Release under
`var.release_version` at plan time. If the tag does not exist yet the
`data "http"` block will 404 and plan will fail — that's the intended
behaviour, preventing a phantom deploy.

## Rollback

```bash
terraform apply -var release_version=<prior-tag>
```

Wire schema is additive (widens the `identity_details` discriminated
union with `GCPIdentityDetails`) — downstream SlashID processing is
compatible either way.

## Requirements

- Terraform >= 1.5
- `google` provider >= 6.0
- `gcloud` CLI available on the machine running `terraform apply` (the
  `setPublisherModelConfig` step drives gcloud via `local-exec`; native
  TF resource lands when the provider adds it).
- APIs enabled by the module: aiplatform, bigquery, cloudbuild,
  cloudfunctions, cloudscheduler, eventarc, firestore, logging, pubsub,
  run, secretmanager, storage.

## Naming

Every provisioned identifier is prefixed:

- `slashid_vertex_` (underscore) — BigQuery, Secret Manager, Firestore.
- `slashid-vertex-` (hyphen) — GCS bucket, Cloud Function, Pub/Sub,
  Scheduler, Service Account.

Override individually via the naming variables in `variables.tf`.
