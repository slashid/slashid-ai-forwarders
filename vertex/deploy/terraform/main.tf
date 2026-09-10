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
  # Maintained Model Garden catalog — refreshed via ``refresh_models.sh``.
  # ``all_models`` is every publisher/model entry we know about;
  # ``gemini_models`` is filtered to ``google/gemini-*`` (BQ payload
  # path was Gemini-only through Phase 3.6).
  all_models    = jsondecode(file("${path.module}/all_models.json"))
  gemini_models = [for m in local.all_models : m if startswith(m, "google/gemini-")]

  # Phase 3.7: default expands to every catalogued model. Google
  # entries drive the BQ payload path (setPublisherModelConfig +
  # per-model table); non-Google entries drive the audit-log-only
  # path (AuditOnlyEventSource client-side filter).
  effective_observed_models = coalesce(var.observed_models, local.all_models)

  google_observed = [for m in local.effective_observed_models : m if startswith(m, "google/")]
  audit_observed  = [for m in local.effective_observed_models : m if !startswith(m, "google/")]

  # Split "publisher/model" entries — Google side drives BQ table
  # naming and setPublisherModelConfig calls; the audit side is
  # passed to the CF as an env var without needing per-model TF
  # resources.
  google_observed_models = {
    for m in local.google_observed :
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

  # Release artefact naming. Matches bedrock's convention
  # ``slashid-<vendor>-forwarder-v<X.Y.Z>.zip``, which drops the
  # tag prefix — hence the ``trimprefix`` step.
  release_version_short = trimprefix(var.release_version, "vertex-")
  release_zip_filename  = "slashid-vertex-forwarder-${local.release_version_short}.zip"
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
