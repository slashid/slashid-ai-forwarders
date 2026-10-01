# Service account + role grants.
#
# The Cloud Run service runs as this SA (never the default compute SA);
# every grant below is the minimum needed for one polling tick:
#
#   - BigQuery: read the request-response tables, run the polling query.
#   - Firestore: read/write the checkpoint document.
#   - Secret Manager: fetch the push-token secret at cold start.
#   - Vertex: setPublisherModelConfig on each configured model
#     (called by the null_resource in bigquery.tf via gcloud).

resource "google_service_account" "forwarder" {
  account_id   = var.service_account_id
  display_name = "SlashID Vertex forwarder"
  description  = "Runs the Cloud Run service polling Vertex request-response logs and pushing to SlashID."
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

# ``logging.list_entries()`` reads audit log entries directly through
# the Cloud Logging API. Data Access audit logs (which is what Vertex
# Gemini ``Generate*Content`` calls produce) are considered private
# and require ``roles/logging.privateLogViewer`` — ``roles/logging.viewer``
# alone returns zero entries for Data Access reads (silent, not a
# permission error).
resource "google_project_iam_member" "logging_viewer" {
  project = var.project_id
  role    = "roles/logging.privateLogViewer"
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
