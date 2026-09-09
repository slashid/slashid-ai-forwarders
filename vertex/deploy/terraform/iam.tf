# Service account + role grants.
#
# The Cloud Function runs as this SA (never the default compute SA);
# every grant below is the minimum needed for one polling tick:
#
#   - BigQuery: read the request-response tables, run the polling query.
#   - Firestore: read/write the checkpoint document.
#   - Secret Manager: fetch the push-token secret at cold start.
#   - Vertex: setPublisherModelConfig on each configured model
#     (called by the null_resource in bigquery.tf via gcloud).
#   - Pub/Sub: consume from the trigger topic (Cloud Function 2nd gen
#     requires this even when Eventarc mediates delivery).

resource "google_service_account" "forwarder" {
  account_id   = var.service_account_id
  display_name = "SlashID Vertex forwarder"
  description  = "Runs the Cloud Function polling Vertex request-response logs and pushing to SlashID."
}

resource "google_project_iam_member" "bigquery_data_viewer" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.forwarder.email}"
}

resource "google_project_iam_member" "bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.forwarder.email}"
}

resource "google_project_iam_member" "datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.forwarder.email}"
}

resource "google_project_iam_member" "secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.forwarder.email}"
}

# Pub/Sub topic invoker — required for the CloudEvent trigger to route
# scheduler ticks into the function.
resource "google_project_iam_member" "pubsub_subscriber" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${google_service_account.forwarder.email}"
}

# Cloud Function invocation grant for the Eventarc service agent —
# needed by 2nd gen Cloud Function + Pub/Sub trigger delivery.
resource "google_project_iam_member" "run_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.forwarder.email}"
}

# fileData bucket access — grants roles/storage.objectViewer on either
# every bucket in the project (var.filedata_buckets == ["*"]) or the
# specific buckets listed. Empty list (default) → no grants, fileData
# resolver falls back to stub entries.
locals {
  filedata_wildcard      = length(var.filedata_buckets) == 1 && var.filedata_buckets[0] == "*"
  filedata_bucket_grants = local.filedata_wildcard ? toset([]) : toset(var.filedata_buckets)
}

resource "google_project_iam_member" "filedata_project_wide" {
  count   = local.filedata_wildcard ? 1 : 0
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.forwarder.email}"
}

resource "google_storage_bucket_iam_member" "filedata_per_bucket" {
  for_each = local.filedata_bucket_grants
  bucket   = each.value
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${google_service_account.forwarder.email}"
}
