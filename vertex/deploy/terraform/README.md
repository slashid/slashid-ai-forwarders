# SlashID Vertex forwarder — Terraform module

Provisions the customer-side GCP resources for the Vertex AI forwarder
Cloud Function:

- One BigQuery dataset PER observed region + one table per logged
  publisher model within each dataset.
- `setPublisherModelConfig` on each (region, model) pair so Vertex
  routes request-response logs into the matching regional dataset.
- Firestore Native database (optional — reuse an existing one by
  setting `create_database = false`).
- Cloud Function 2nd gen (source zip fetched from GitHub Releases).
  Deployed to a single region (the first non-`global` entry in
  `regions`); observes every region in `regions` via API calls.
- Cloud Scheduler cron → Pub/Sub topic → Cloud Function trigger.
- Secret Manager entry for the SlashID push token.
- Service account with least-privilege role grants.
- Log-sink exclusion for Vertex Data Access audit logs (cost mitigation
  — the underlying capture stays enabled for future correlation work).

## Usage

Single region — the Cloud Function, Firestore, Scheduler, and BigQuery
dataset all live in the one region:

```hcl
module "slashid_vertex_forwarder" {
  source = "github.com/slashid/slashid-ai-forwarders//vertex/deploy/terraform?ref=vertex-v0.1.9"

  project_id         = "customer-project-123456"
  regions            = ["us-central1"]
  slashid_endpoint   = "https://api.slashid.com"
  slashid_push_token = var.slashid_push_token # sensitive
  release_version    = "vertex-v0.1.9"
}
```

Multi-region — one BigQuery dataset per entry, all sharing a single
Cloud Function whose audit-log filter OR's the per-region matches.
The CF itself, Firestore, and Cloud Scheduler deploy to the first
non-`global` entry:

```hcl
module "slashid_vertex_forwarder" {
  source = "github.com/slashid/slashid-ai-forwarders//vertex/deploy/terraform?ref=vertex-v0.1.9"

  project_id      = "customer-project-123456"
  regions         = ["us-central1", "europe-west1", "asia-northeast1"]
  observed_models = ["google/gemini-2.5-flash", "anthropic/claude-sonnet-4-5"]
  # ...
}
```

`observed_models` defaults to the module's full catalog
(`all_models.json`) — every catalogued model on every region in
`regions`. Set explicitly to narrow scope.

### Observing `global`

`regions` accepts `"global"`, which observes Vertex's global endpoint —
what Cloud Console and Vertex AI Studio target. That traffic is the
clearest "human at the keyboard" signal and is invisible to a purely
regional deployment.

```hcl
regions = ["us-central1", "europe-west1", "global"]
```

Two properties to accept first. Google routes a global request to
whichever region has capacity and never discloses which, so there is no
data-residency guarantee — a deployment with residency constraints
should not be using the global endpoint at all. And BigQuery has no
`global` location, so global's dataset is created in the deployment
region; reordering `regions` so the deployment region changes would
move it, and since dataset location is immutable Terraform would
destroy and recreate it, losing unprocessed rows.

At least one entry must not be `global` — the Cloud Function, Firestore
and Scheduler need somewhere to live.

Per-region datasets are named `{bq_dataset_prefix}_{region_slug}`,
where `region_slug` replaces `-` with `_` (BQ dataset IDs disallow
`-`). Example: `slashid_vertex_reqresp_logs_us_central1`.

`slashid_push_token` is sensitive — declare it as a sensitive
variable in your root module and source it from a secret manager
(not `.tfvars` committed to VCS).

The token is read only when its secret version is created, so a later
apply leaves it alone whatever value it passes. To rotate it, replace
the version with the new value:
`terraform apply -replace='module.slashid_vertex_forwarder.google_secret_manager_secret_version.push_token'`.

## `fileData` bucket grants

Gemini `fileData` (`gs://bucket/object`) attachments need the
forwarder SA to hold `roles/storage.objectViewer` on the referenced
buckets. Configure via `filedata_buckets`:

```hcl
# Named list — grants per-bucket. Preferred when sources are known.
filedata_buckets = ["customer-uploads", "vertex-context"]

# Wildcard — grants project-wide (covers every bucket in project_id).
# Use when fileData sources aren't fixed to a known set.
filedata_buckets = ["*"]

# Default: empty — no grants. fileData attachments still parse but
# emit stubs (URI + media_type only, no md5, no byte_length).
filedata_buckets = []
```

Unlisted or unreadable buckets emit stub `AIAccessedFile` entries
instead of failing the tick. Cross-project buckets need customer-
managed IAM (this module only grants against `project_id`); the
forwarder stubs them if inaccessible.

## Model catalog

`all_models.json` is a maintained snapshot of Vertex Model Garden's
publisher catalog. Terraform reads it at plan time — no `gcloud`
call, deterministic plans. When `observed_models` is omitted the
module observes every catalogued model on every `regions` entry;
Google entries get the BQ payload path (per-region
`setPublisherModelConfig` + per-region table), and every entry —
Google included — participates in the audit-only path (via the
`NOT publishers/google/ OR status.code!=0` server-side filter that
captures errored Google calls the BQ path drops).

Refresh the catalog via `./refresh_models.sh --project <GCP_PROJECT>`
(needs bash, gcloud, jq). A scheduled workflow at
`.github/workflows/vertex-model-catalog-refresh.yml` runs this
weekly and opens a PR when the catalog moves.

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

Rolling back across the multi-region rename is destructive — the
singular `var.region` and dataset ID `slashid_vertex_reqresp_logs`
were removed. A downgrade to a pre-multi-region tag would try to
recreate that dataset alongside the per-region ones; back up any
retained rows before rolling back.

## Requirements

- Terraform >= 1.5
- `google` provider >= 6.0
- `gcloud` CLI + `curl` on the machine running `terraform apply` — the
  `setPublisherModelConfig` step calls the Vertex `v1beta1` REST
  endpoint via `curl`, using `gcloud auth print-access-token` for the
  bearer token. No native TF resource / stable gcloud subcommand
  exists for this API yet; the module will swap over as soon as one
  ships.
- The `gcloud` principal must hold `aiplatform.endpoints.setPublisherModelConfig`
  on the project. Predefined roles that include it: `roles/aiplatform.admin`
  and `roles/owner`. `roles/aiplatform.user` and `roles/aiplatform.viewer`
  do **not**.
- `gh` CLI authenticated for `slashid/slashid-ai-forwarders` on the
  machine running `terraform apply` — the release-zip download step
  uses `gh release download` (the repo is private today; a bare
  `curl` against the releases URL is anonymous and 404s). Verify
  with `gh auth status`.
- APIs enabled by the module: aiplatform, bigquery, cloudbuild,
  cloudfunctions, cloudscheduler, eventarc, firestore, logging, pubsub,
  run, secretmanager, storage.

## Naming

Every provisioned identifier is prefixed:

- `slashid_vertex_` (underscore) — BigQuery, Secret Manager, Firestore.
- `slashid-vertex-` (hyphen) — GCS bucket, Cloud Function, Pub/Sub,
  Scheduler, Service Account.

Override individually via the naming variables in `variables.tf`.
