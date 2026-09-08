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
  # ``gemini_models`` is filtered to ``google/gemini-*`` (the subset
  # phase 3.1 can actually forward).
  all_models    = jsondecode(file("${path.module}/all_models.json"))
  gemini_models = [for m in local.all_models : m if startswith(m, "google/gemini-")]

  # Effective ``observed_models``: caller override wins; otherwise
  # default to every currently-catalogued Gemini model. Callers who
  # want a broader scope can pass ``jsondecode(file("all_models.json"))``
  # explicitly, or their own curated list.
  effective_observed_models = coalesce(var.observed_models, local.gemini_models)

  # Split "publisher/model" entries into their two segments up-front —
  # both the BQ table naming and the setPublisherModelConfig REST call
  # need the pair. Table slug replaces the separators BQ rejects.
  observed_models = {
    for m in local.effective_observed_models :
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
