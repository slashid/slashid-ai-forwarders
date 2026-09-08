# Provider + shared locals.
#
# The module runs against the customer's own GCP project — no SlashID
# infrastructure sits between the customer and their data. Every
# resource identifier follows the ``slashid_vertex_`` prefix convention
# (underscore for BQ / Secret Manager / Firestore, hyphen for GCS /
# Cloud Function / Pub/Sub / Scheduler / SA per GCP naming rules).

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  # Split "publisher/model" entries into their two segments up-front —
  # both the BQ table naming and the setPublisherModelConfig REST call
  # need the pair. Table slug replaces the separators BQ rejects.
  logged_models = {
    for m in var.logged_publisher_models :
    replace(replace(m, "/", "_"), ".", "_") => {
      publisher = split("/", m)[0]
      model     = split("/", m)[1]
      full      = m
    }
  }

  # GCS bucket names are global — default suffix keeps first-time
  # deployments from colliding across customer projects.
  release_bucket = coalesce(
    var.release_bucket_name,
    "slashid-vertex-release-${var.project_id}"
  )

  # Source zip URL from GitHub Releases. TF's `data "http"` block reads
  # this at plan time and streams it into the staging bucket.
  release_zip_url = "https://github.com/${var.release_repo}/releases/download/${var.release_version}/slashid-vertex-forwarder-${var.release_version}.zip"
}

# ---------------------------------------------------------------------------
# API enablement
# ---------------------------------------------------------------------------
#
# ``disable_on_destroy = false`` — leaving APIs enabled after `terraform
# destroy` is safer for shared projects (other workloads may depend on
# them). The APIs are effectively free once enabled.

resource "google_project_service" "required" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "bigquery.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudscheduler.googleapis.com",
    "eventarc.googleapis.com",
    "firestore.googleapis.com",
    "logging.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

# Audit config: Vertex Data Access logs. Not required for the v1 BQ-only
# path but enabled so it's ready when the correlation phase joins BQ +
# audit-log records into per-invocation identity.
resource "google_project_iam_audit_config" "vertex_data_access" {
  project = var.project_id
  service = "aiplatform.googleapis.com"

  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }

  depends_on = [google_project_service.required]
}
