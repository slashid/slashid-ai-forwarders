# SlashID Anthropic forwarder — Terraform module

Provisions the customer-side GCP resources for the Claude Enterprise
receiver:

- One Cloud Run v2 service with two routes: the Inference hooks endpoint
  (which also answers allow/deny) and `POST /tick`.
- Cloud Scheduler job firing `POST /tick` under an OIDC token, minted for
  a service account whose only privilege is invoking this one service.
- A named Firestore database (`slashid-anthropic`), the composite index
  the deadline sweep needs, and the TTL policy on `tombstone_expires_at`.
- Up to three Secret Manager entries — the push token always, the hook
  signing secret and the compliance key when supplied.
- A least-privilege runtime service account.
- An Artifact Registry **remote repository** proxying `ghcr.io`, because
  Cloud Run pulls from Artifact Registry and nowhere else.

Nothing in this module serializes ticks. Cloud Run hands a second
concurrent `POST /tick` to a second instance, and
`max_instance_request_concurrency` is about in-instance load, not mutual
exclusion. The tick's Firestore lease is what makes overlap safe.

## Capabilities

Which half of the receiver runs is decided by which credentials are
present, not by a mode flag. At least one is required; the service
refuses to start with neither, and a `lifecycle` precondition here says
so before the apply reaches Cloud Run.

### Hook only

```hcl
module "slashid_anthropic_forwarder" {
  source = "git::https://github.com/slashid/slashid-ai-forwarders.git//anthropic/deploy/terraform?ref=anthropic-v0.1.0"

  project_id         = "customer-project-123456"
  region             = "us-central1"
  slashid_endpoint   = "https://api.slashid.com"
  slashid_push_token = var.slashid_push_token # sensitive
  release_version    = "anthropic-v0.1.0"

  hook_signing_secret = var.hook_signing_secret # whsec_… , sensitive

  # While the image package is private:
  ghcr_username = var.ghcr_username
  ghcr_token    = var.ghcr_token # classic token, scope read:packages
}
```

### Compliance only

No public endpoint, no certificate, no warm instance — ingress is
internal-only and `min_instance_count` is pinned to 0 regardless of
`min_instances`.

```hcl
module "slashid_anthropic_forwarder" {
  source = "git::https://github.com/slashid/slashid-ai-forwarders.git//anthropic/deploy/terraform?ref=anthropic-v0.1.0"

  project_id         = "customer-project-123456"
  slashid_endpoint   = "https://api.slashid.com"
  slashid_push_token = var.slashid_push_token
  release_version    = "anthropic-v0.1.0"

  compliance_key    = var.compliance_key # sk-ant-api01-… , sensitive
  organization_uuid = "11111111-1111-1111-1111-111111111111"
}
```

`organization_uuid` is required with `compliance_key`: the key can read
every linked organization, so the readers filter to one.

### Both

```hcl
module "slashid_anthropic_forwarder" {
  source = "git::https://github.com/slashid/slashid-ai-forwarders.git//anthropic/deploy/terraform?ref=anthropic-v0.1.0"

  project_id         = "customer-project-123456"
  slashid_endpoint   = "https://api.slashid.com"
  slashid_push_token = var.slashid_push_token
  release_version    = "anthropic-v0.1.0"

  hook_signing_secret = var.hook_signing_secret
  compliance_key      = var.compliance_key
  organization_uuid   = "11111111-1111-1111-1111-111111111111"
}
```

One deployment shares one push token by construction. Splitting the hook
and the readers across two deployments loses the join — two deployments
cannot share a pending store — and splitting them across two SlashID
connections double-counts every invocation both halves saw, because the
terminal's dedup key is `{org}:{conn}:{request_id}`.

## The image

The release workflow publishes
`ghcr.io/slashid/slashid-anthropic-forwarder:<version>` and the module
derives the tag from `release_version` by dropping the `anthropic-v`
prefix: `anthropic-v0.1.0` → `:0.1.0`. Cloud Run then pulls it through
the Artifact Registry remote repository this module creates.

While the repository — and therefore its package — is private,
`ghcr_username` and `ghcr_token` are **required**: without them the proxy
has no upstream credential and every revision fails to pull. The token is
stored in Secret Manager and read by Artifact Registry's own service
agent, not by the runtime service account.

Set `image` to a full reference to run a locally built image instead;
`release_version` is then ignored for the tag.

## The tick cadence, and the inequality it is part of

`tick_interval_seconds` is a **number**, not a cron string: the service
compares it against `tombstone_ttl_seconds` at startup and cannot compare
a cron expression to a number. The module derives the unix-cron schedule
from it (`tick_schedule` is an output), so the number is the input and
the cron is the derivation. It must divide an hour or be a whole number
of hours dividing a day; Cloud Scheduler has no sub-minute granularity,
so 60 is the tightest cadence.

    tombstone_ttl_seconds > join_wait_seconds + poll_lag_seconds + tick_interval_seconds

A tombstone that expires before the reader arrives lets that reader
re-emit an invocation that was already pushed. The service asserts the
same inequality at startup and refuses to run, so a violation is a failed
revision rather than a silent duplicate — but a `lifecycle` precondition
here catches it at plan time instead.

The defaults leave 3480 s of tick interval (7200 − 3600 − 120), so a
hook-only deployment cannot run an hourly tick without raising
`tombstone_ttl_seconds`.

## Secrets are write-once

Each secret version is created with the value of its variable and never
updated by a later apply, so an apply that passes a different value (or
a placeholder) leaves the stored secret alone. To rotate one, replace its
version and pass the new value:

| Secret | Version to replace |
| --- | --- |
| `slashid_push_token` | `google_secret_manager_secret_version.push_token` |
| `hook_signing_secret` | `google_secret_manager_secret_version.signing_secret[0]` |
| `compliance_key` | `google_secret_manager_secret_version.compliance_key[0]` |
| `ghcr_token` | `google_secret_manager_secret_version.ghcr_token[0]` |

For a signing-secret rotation, comma-join the old and new `whsec_…` in
`hook_signing_secret` so both are accepted until the old one is retired.

## Setting up the hook in claude.ai

The signing secret does not exist until the endpoint is configured, and
the endpoint does not exist until this module is applied. So it is two
applies:

1. Apply with a placeholder `hook_signing_secret` (any `whsec_…` value;
   every frame gets 401 until step 3). Copy the `hook_url` output.
2. In claude.ai, as an Owner or Primary owner (`organization:manage`),
   configure that URL as the Inference hooks endpoint. It must be
   `https://` on port 443, publicly routable, with a valid public CA
   certificate, no redirects and no reverse tunnels.
3. Take the generated `whsec_…` and replace the secret version with it
   (secrets are write-once, see below):
   `terraform apply -replace='google_secret_manager_secret_version.signing_secret[0]'`
   with `hook_signing_secret` set.
4. Use claude.ai's **Test connection** to confirm the receiver answers.
5. Then claude.ai's own staged rollout: shadow mode, a rollout
   percentage, role exclusions, and finally enforcement with your choice
   of fail-open or fail-closed.

Leave `shadow_mode = true` here — ours, distinct from claude.ai's, and
when either is on nothing is blocked — until the customer opts in.

## Firestore

The database is named rather than `(default)`, the same isolation
`vertex/` takes. Firestore databases cannot be undeleted, so
`deletion_policy = "ABANDON"`: a `terraform destroy` leaves it behind,
and a re-apply into the same project should set
`create_database = false`.

The TTL policy keys on `tombstone_expires_at`, the field the store writes
as `tombstoned_at + tombstone_ttl_seconds`. A live record never carries
it, so the policy cannot reach one — which matters: expiring by creation
time would delete a record that had been failing to push, the exact loss
the store exists to prevent.

## Frame capture

Off unless `capture_bucket` names one, and meant to stay off. A prompt
frame is the customer's whole transcript in plaintext, so a bucket named
here accumulates their conversations.

It exists because the protocol is only partly documented. Every wire
claim this forwarder relies on was settled by reading real frames rather
than the docs: that a hook spells the tool name differently from the
Messages API, that attachments arrive as extracted text with no bytes,
that one session id carries a hundred interleaved sub-conversations, and
that the two sources share no identifier but a tool-use id. The
measurements in `anthropic/README.md` came from a capture like this one.

Turn it on against a test tenant, for as long as it takes to answer a
question, and give the bucket a retention policy. The service account
gets `objectCreator` and nothing in the service reads an object back.

`capture_deny_marker` is the sibling knob: a literal string that forces a
deny, for exercising enforcement. Prefer `SLASHID_MOCK_DENIED_HASHES`,
which is content-addressed — a marker is tripped by anyone who merely
quotes it, including the person testing it.

## Outputs

| output | what it is |
| --- | --- |
| `hook_url` | Configure this as the Inference hooks endpoint. Empty when the hook is disabled. |
| `service_uri` | Cloud Run base URL; `POST /tick` under it is what the scheduler calls. |
| `service_account_email` | The runtime service account. |
| `scheduler_service_account_email` | The identity the OIDC token names. |
| `tick_schedule` | The unix-cron schedule derived from `tick_interval_seconds`. |
| `image` | The image the service runs, through the registry proxy. |
| `database` | The named database holding records and checkpoints. |
| `capabilities` | `{ hook, compliance }` — which halves this deployment runs. |
