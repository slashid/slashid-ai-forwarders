# --- Required customer inputs ---------------------------------------------

variable "project_id" {
  description = "GCP project ID that hosts Vertex + this forwarder."
  type        = string
}

variable "region" {
  description = "GCP region for the Cloud Function, BigQuery dataset, and Vertex publisher-model config."
  type        = string
}

variable "observed_models" {
  description = <<-EOT
    Vertex publisher models whose invocations the forwarder should
    observe and push to SlashID. Entries take the form "publisher/model"
    (e.g. "google/gemini-2.5-flash", "google/gemini-2.5-pro").

    Under the hood the module enables Vertex request-response logging
    (setPublisherModelConfig) on each model and routes the logs into a
    per-model BigQuery table the polling function reads from. The
    "observed" framing keeps the caller decoupled from that plumbing —
    a future push-based delivery could swap in without renaming.

    When omitted (or null), defaults to every ``google/gemini-*`` entry
    in the module's shipped catalog (``all_models.json``, refreshed
    via ``refresh_models.sh``). Set explicitly to narrow the scope
    (e.g. only production models) or to include a non-Gemini publisher
    once phase 3.3+ ships rawPredict support.

    Note: setPublisherModelConfig propagation takes ~10 min after apply
    for a first-time enablement — the first BQ row may take that long
    to appear.
  EOT
  type        = list(string)
  default     = null

  validation {
    condition = (
      var.observed_models == null || alltrue([
        for m in coalesce(var.observed_models, []) : length(split("/", m)) == 2
      ])
    )
    error_message = "Each entry in observed_models must be in \"publisher/model\" form (e.g. \"google/gemini-2.5-flash\")."
  }
}

variable "slashid_endpoint" {
  description = "SlashID base URL for the NHI events endpoint (e.g. https://api.slashid.com)."
  type        = string
}

variable "slashid_push_token" {
  description = "Bearer token for the SlashID push connection. Sensitive — stored in Secret Manager."
  type        = string
  sensitive   = true
}

# --- Optional runtime knobs -----------------------------------------------

variable "include_raw_content" {
  description = "When true, the Cloud Function forwards prompt/response bodies (redacted_text/redacted_content). Off by default — hash + mime + byte_length still ship."
  type        = bool
  default     = false
}

variable "max_content_size" {
  description = "Character cap on redacted_text/redacted_content when include_raw_content is true."
  type        = number
  default     = 100000
}

variable "poll_cadence_seconds" {
  description = "How often the Cloud Scheduler fires the polling function. 60s balances freshness against BQ query cost."
  type        = number
  default     = 60
}

variable "max_rows_per_tick" {
  description = "Upper bound on BQ rows processed per tick — prevents runaway batches on backlog."
  type        = number
  default     = 1000
}

variable "request_timeout_seconds" {
  description = "HTTP client timeout for the push to SlashID."
  type        = number
  default     = 10
}

# --- Deployment knobs ------------------------------------------------------

variable "release_version" {
  description = "Vertex forwarder release tag (e.g. \"vertex-v0.1.0\") — the module fetches the source zip from GitHub Releases under this tag."
  type        = string
}

variable "release_repo" {
  description = "GitHub repo hosting the release artefacts. Override for forks."
  type        = string
  default     = "slashid/slashid-ai-forwarders"
}

variable "create_firestore_database" {
  description = <<-EOT
    Provision the Firestore Native-mode database. Firestore is
    singleton-per-project until multi-database GA — set to false to
    reuse an existing Firestore in this project.
  EOT
  type        = bool
  default     = true
}

variable "log_level" {
  description = "Python logging level for the Cloud Function."
  type        = string
  default     = "INFO"
}

# --- Naming overrides (all defaults follow the ``slashid_vertex_`` /
#     ``slashid-vertex-`` prefix convention) --------------------------------

variable "bq_dataset_id" {
  description = "BigQuery dataset holding one table per logged publisher model."
  type        = string
  default     = "slashid_vertex_reqresp_logs"
}

variable "firestore_database" {
  description = "Named Firestore database (multi-database GA). Kept isolated from the project's (default) database so the forwarder does not interfere with other customer workloads."
  type        = string
  default     = "slashid-vertex"
}

variable "firestore_checkpoint_collection" {
  description = "Firestore collection for the polling checkpoint document."
  type        = string
  default     = "slashid_vertex"
}

variable "firestore_checkpoint_document" {
  description = "Firestore document holding the polling checkpoint."
  type        = string
  default     = "checkpoint"
}

variable "secret_id" {
  description = "Secret Manager secret ID storing the SlashID push token."
  type        = string
  default     = "slashid_vertex_push_token"
}

variable "function_name" {
  description = "Cloud Function name."
  type        = string
  default     = "slashid-vertex-forwarder"
}

variable "service_account_id" {
  description = "Cloud Function service account short ID."
  type        = string
  default     = "slashid-vertex-sa"
}

variable "scheduler_name" {
  description = "Cloud Scheduler job name for the polling tick."
  type        = string
  default     = "slashid-vertex-scheduler"
}

variable "trigger_topic_name" {
  description = "Pub/Sub topic that Cloud Scheduler publishes to and the function consumes."
  type        = string
  default     = "slashid-vertex-trigger"
}

variable "release_bucket_name" {
  description = "GCS bucket for the release zip. Defaults to slashid-vertex-release-<project_id> for global uniqueness."
  type        = string
  default     = ""
}
