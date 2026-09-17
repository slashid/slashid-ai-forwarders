# Provider + shared locals.
#
# The module runs against the customer's own GCP project — no SlashID
# infrastructure sits between the customer and their data. Every
# resource identifier follows the ``slashid_vertex_`` prefix convention
# (underscore for BQ / Secret Manager / Firestore, hyphen for GCS /
# Cloud Function / Pub/Sub / Scheduler / SA per GCP naming rules).

provider "google" {
  project = var.project_id
  region  = local.deployment_region
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
  # per-model table); every entry — Google included — drives the
  # audit-log-only path too, because errored Google calls are dropped
  # by response-conditional payload logging and only the audit path
  # sees them. The audit source's server-side filter
  # (``non-Google OR protoPayload.status.code!=0``) prevents double-counting.
  effective_observed_models = coalesce(var.observed_models, local.all_models)

  google_observed = [for m in local.effective_observed_models : m if startswith(m, "google/")]

  # The Cloud Function, Firestore, Scheduler, and release bucket all
  # deploy to the first NON-GLOBAL region in ``var.regions`` — a single
  # physical home. ``global`` is a Vertex routing target, not a place
  # that can host them. Vertex regions observed are the full list;
  # deployment region is a separate concern (where the CF lives, not
  # what it queries).
  deployment_region = [for r in var.regions : r if r != "global"][0]

  # ``global`` has no location of its own, so its dataset lives with the
  # deployment. Regional entries stay pinned to their own region — a
  # customer observing ``europe-west1`` has prompt and response bodies
  # resting there, and consolidating would silently relocate them.
  dataset_location = { for r in var.regions : r => r == "global" ? local.deployment_region : r }

  # The global endpoint is the unprefixed host; there is no
  # ``global-aiplatform.googleapis.com``.
  vertex_host = {
    for r in var.regions : r => r == "global" ? "aiplatform.googleapis.com" : "${r}-aiplatform.googleapis.com"
  }

  # BQ dataset IDs can't contain ``-``; the CF applies the same
  # transform when deriving dataset names from ``config.gcp_regions``.
  region_slugs = { for r in var.regions : r => replace(r, "-", "_") }

  # Split "publisher/model" entries — Google side drives BQ table
  # naming and setPublisherModelConfig calls. Multi-region: the
  # Cartesian product of ``(region, google_observed_model)`` — each
  # (region, model) pair gets its own BQ table + own
  # ``setPublisherModelConfig`` call.
  #
  # Resource key (``<region_slug>__<model_slug>``) uniquely identifies
  # the (region, model) pair for TF. ``model_slug`` is the within-
  # dataset table suffix — no region prefix because the dataset is
  # already regional; ``BqEventSource`` reads via wildcard
  # ``slashid_vertex_reqresp_*`` within a single dataset.
  google_observed_models = {
    for pair in setproduct(var.regions, local.google_observed) :
    "${local.region_slugs[pair[0]]}__${replace(replace(pair[1], "/", "_"), ".", "_")}" => {
      region     = pair[0]
      publisher  = split("/", pair[1])[0]
      model      = split("/", pair[1])[1]
      full       = pair[1]
      model_slug = replace(replace(pair[1], "/", "_"), ".", "_")
      dataset_id = "${var.bq_dataset_prefix}_${local.region_slugs[pair[0]]}"
    }
  }

  # Per-region datasets. One entry per region; each dataset holds every
  # observed Google model's per-model table.
  regional_datasets = {
    for r in var.regions :
    r => "${var.bq_dataset_prefix}_${local.region_slugs[r]}"
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
