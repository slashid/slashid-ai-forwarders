# --- Required customer inputs ---------------------------------------------

variable "project_id" {
  description = "GCP project that hosts the receiver, its Firestore database and its secrets."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud Scheduler, Artifact Registry and Firestore."
  type        = string
  default     = "us-central1"
}

variable "slashid_endpoint" {
  description = "SlashID base URL for the NHI events endpoint."
  type        = string
  default     = "https://api.slashid.com"
}

variable "slashid_push_token" {
  description = "Push token of the SlashID ``anthropic`` connection. Sensitive — stored in Secret Manager. One deployment shares one token by construction; splitting the hook and the readers across deployments loses the join, and splitting them across connections double-counts every invocation both halves saw. Read only when the secret version is created or replaced; later values are ignored."
  type        = string
  sensitive   = true
}

variable "release_version" {
  description = "Receiver release tag (e.g. \"anthropic-v0.1.0\"). The image tag is the bare version."
  type        = string
}

# --- Capabilities ----------------------------------------------------------
#
# At least one of ``hook_signing_secret`` and ``compliance_key`` must be
# set: they are what the service's own startup check calls a capability,
# and with neither it refuses to start.

variable "hook_signing_secret" {
  description = "whsec_… generated when the Inference hooks endpoint is configured. Comma-join any number to accept them all during a rotation. Empty disables the hook. Sensitive. Read only when the secret version is created or replaced; later values are ignored."
  type        = string
  default     = ""
  sensitive   = true
}

variable "compliance_key" {
  description = "Compliance Access Key (sk-ant-api01-…) with read:compliance_activities and read:compliance_user_data. Empty disables the readers. Sensitive. Read only when the secret version is created or replaced; later values are ignored."
  type        = string
  default     = ""
  sensitive   = true
}

variable "organization_uuid" {
  description = "Required with compliance_key: the key can read every linked organization, so the readers filter to this one."
  type        = string
  default     = ""
}

# --- Verdict knobs ---------------------------------------------------------

variable "preflight_enabled" {
  description = "Call {slashid_endpoint}/ip/nhi/events/ai-invocations/preflight on every prompt and tool call: the sensitive-file check and the connection's AI policy."
  type        = bool
  default     = true
}

variable "verdict_fail_mode" {
  description = "allow or deny when a check fails or answers unverified. Distinct from Anthropic's own failure handling, which covers the case where this service does not answer at all."
  type        = string
  default     = "allow"

  validation {
    condition     = contains(["allow", "deny"], var.verdict_fail_mode)
    error_message = "verdict_fail_mode must be \"allow\" or \"deny\"."
  }
}

variable "shadow_mode" {
  description = "Our own shadow mode, named after claude.ai's and independent of it: when either is on, nothing is blocked. False by default: the receiver enforces."
  type        = bool
  default     = false
}

variable "verdict_budget_ms" {
  description = "Both checks run concurrently under this budget, inside Anthropic's configured verdict timeout."
  type        = number
  default     = 3500
}

variable "push_budget_ms" {
  description = "Bounds the background push so a hung sink cannot pin an instance. Never delays a verdict."
  type        = number
  default     = 2000
}

# --- Pending store ---------------------------------------------------------

variable "join_wait_seconds" {
  description = "Deadline before an unsettled record is pushed as it stands. A push is a commitment the terminal's dedup will not top up, so this sits beyond normal reader lag rather than being trimmed for latency."
  type        = number
  default     = 3600
}

variable "tombstone_ttl_seconds" {
  description = <<-EOT
    How long a pushed record's tombstone suppresses a late reader's
    duplicate. Must exceed join_wait_seconds + poll_lag_seconds +
    tick_interval_seconds — the service asserts the same inequality at
    startup and refuses to run if it fails, so a violation here is a failed
    revision rather than a silent re-emission.

    The defaults leave 3480 s of tick interval (7200 − 3600 − 120), so a
    hook-only deployment cannot run an hourly tick without raising this.
  EOT
  type        = number
  default     = 7200
}

variable "max_flushes_per_tick" {
  description = "Bounds the ``due`` query so one tick cannot stall behind a backlog."
  type        = number
  default     = 500
}

variable "poll_lag_seconds" {
  description = "How far behind now the readers' updated_at.gte bound sits."
  type        = number
  default     = 120
}

variable "max_sessions_per_tick" {
  description = "Bounds a tick against the 600 rpm the Compliance API shares with the sync adapter."
  type        = number
  default     = 200
}

variable "attachment_hashing" {
  description = "md5 takes the digest the file listing already carries and makes no request; full downloads the bytes and digests them with every algorithm."
  type        = string
  default     = "md5"

  validation {
    condition     = contains(["md5", "full"], var.attachment_hashing)
    error_message = "attachment_hashing must be \"md5\" or \"full\"."
  }
}

variable "max_attachment_fetch_bytes" {
  description = "Under full hashing, the largest attachment worth downloading. Decided from the listing's size_bytes before any fetch; an oversized file keeps the listing's md5 rather than losing its digest."
  type        = number
  default     = 10485760
}

variable "soft_join_window_seconds" {
  description = <<-EOT
    How far from a chat message the soft join looks for a candidate record,
    in seconds. Exactly one candidate in the window enriches it with file
    digests; zero or several abstain. Measured on the reference corpus: 15
    resolves three attachment rounds uniquely, 60 makes one of them
    ambiguous. Raising it trades coverage for the risk of abstaining.
  EOT
  type        = number
  default     = 15
}

# --- Tick cadence ----------------------------------------------------------

variable "tick_interval_seconds" {
  description = <<-EOT
    How often Cloud Scheduler fires ``POST /tick``. The schedule is derived
    from this number rather than taken as a cron string, because the service
    checks ``tombstone_ttl_seconds`` against it and cannot compare a cron
    expression to a number.

    Cloud Scheduler has no sub-minute granularity, so 60 is the tightest
    cadence.
  EOT
  type        = number
  default     = 300

  validation {
    condition = (
      var.tick_interval_seconds >= 60 &&
      var.tick_interval_seconds % 60 == 0 &&
      (
        var.tick_interval_seconds <= 3600
        ? 3600 % var.tick_interval_seconds == 0
        : (var.tick_interval_seconds % 3600 == 0 && 86400 % var.tick_interval_seconds == 0)
      )
    )
    error_message = "tick_interval_seconds must divide an hour (60, 120, 180, 240, 300, 360, 600, 720, 900, 1200, 1800, 3600) or be a whole number of hours dividing a day (7200, 10800, 14400, 21600, 28800, 43200, 86400): the unix-cron schedule is derived from it as a step."
  }
}

variable "tick_attempt_deadline_seconds" {
  description = "How long Cloud Scheduler waits for a tick before giving up. Must not exceed service_timeout_seconds."
  type        = number
  default     = 540
}

# --- Service shape ---------------------------------------------------------

variable "memory" {
  description = "Memory per instance. The hook and the tick share an instance, so a tick that exhausts it takes in-flight verdicts down too."
  type        = string
  default     = "1Gi"
}

variable "min_instances" {
  description = "Applies only when the hook is enabled: a cold start inside Anthropic's verdict timeout risks a webhook failure, and enough of those trip its circuit breaker. A compliance-only deployment pins this to 0 regardless."
  type        = number
  default     = 1
}

variable "max_instances" {
  type    = number
  default = 10
}

variable "service_timeout_seconds" {
  description = "Cloud Run request timeout. Sized for the tick, not the hook — a hook verdict is bounded by verdict_budget_ms."
  type        = number
  default     = 600
}

variable "max_body_bytes" {
  description = "Request body cap. Cloud Run caps HTTP/1 bodies at 32 MiB, under the protocol's 64 MiB ceiling; observed frames peak at 1.86 MB."
  type        = number
  default     = 33554432
}

variable "include_raw_content" {
  type    = bool
  default = false
}

variable "max_content_size" {
  type    = number
  default = 100000
}

variable "log_level" {
  type    = string
  default = "INFO"
}

# --- Image + registry ------------------------------------------------------

variable "image" {
  description = "Full image reference. Overrides the one derived from release_version; use for a locally built image."
  type        = string
  default     = ""
}

variable "ghcr_username" {
  description = "GitHub username whose token can read the image package. Required while the repository — and therefore its packages — is private."
  type        = string
  default     = ""
}

variable "ghcr_token" {
  description = "GitHub token (classic, scope read:packages) paired with ghcr_username. Sensitive — stored in Secret Manager for the registry's service agent to read. Read only when the secret version is created or replaced; later values are ignored."
  type        = string
  default     = ""
  sensitive   = true
}

# --- Naming overrides ------------------------------------------------------

variable "create_database" {
  description = "Provision the named Firestore database. False reuses an existing one — Firestore databases are hard to fully delete, so a destroy/re-apply cycle usually leaves one behind."
  type        = bool
  default     = true
}

variable "database" {
  description = "Named Firestore database, kept isolated from the project's (default) database as vertex/ does."
  type        = string
  default     = "slashid-anthropic"
}

variable "pending_collection" {
  description = "Firestore collection holding pending records and their tombstones. The composite index and the TTL policy are provisioned against it."
  type        = string
  default     = "anthropic_pending"
}

variable "checkpoint_collection" {
  description = "Firestore collection holding the readers' three watermark documents. Separate from pending_collection on purpose: that one carries a TTL policy, and a checkpoint is a different lifetime from a pending record."
  type        = string
  default     = "anthropic_checkpoints"
}

variable "service_name" {
  type    = string
  default = "slashid-anthropic-forwarder"
}

variable "service_account_id" {
  type    = string
  default = "slashid-anthropic-sa"
}

variable "scheduler_service_account_id" {
  description = "Service account Cloud Scheduler mints its OIDC token as. Separate from the runtime SA: its only privilege is invoking this one service."
  type        = string
  default     = "slashid-anthropic-tick-sa"
}

variable "scheduler_name" {
  type    = string
  default = "slashid-anthropic-tick"
}

variable "registry_repository_id" {
  description = "Artifact Registry remote repository proxying ghcr.io. Cloud Run pulls from Artifact Registry and nowhere else."
  type        = string
  default     = "slashid-ghcr"
}

variable "hook_path" {
  description = "Path component of the hook URL. Any path works — the receiver answers POST on all of them; this one says what it is."
  type        = string
  default     = "/hooks/anthropic"
}

variable "capture_bucket" {
  description = <<-EOT
    GCS bucket for raw prompt-frame capture, or "" to leave capture off.

    Off by default and meant to stay off: a frame is the customer's whole
    transcript in plaintext, so a bucket named here accumulates their
    conversations. It exists because the protocol is only partly
    documented and the shapes this forwarder relies on were settled by
    reading real frames — the measurements in the README came from a
    capture like this one.

    Enable it on a test tenant, for as long as it takes to answer a
    question, and give the bucket a retention policy. The service account
    is granted objectCreator on it, and nothing in the service ever reads
    an object back.
  EOT
  type        = string
  default     = ""
}

variable "capture_deny_marker" {
  description = <<-EOT
    A literal string that forces a deny when it appears anywhere in a
    frame, for testing enforcement end to end. "" disables it.

    Prefer SLASHID_MOCK_DENIED_HASHES, which is content-addressed: a
    marker is tripped by anyone who merely quotes it, including the
    person testing it, which has wedged a working session before.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}
