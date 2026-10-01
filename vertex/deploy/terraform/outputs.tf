output "service_uri" {
  description = "Cloud Run service HTTPS URL. Only Cloud Scheduler is allowed to call it (internal ingress, scheduler OIDC token)."
  value       = google_cloud_run_v2_service.forwarder.uri
}

output "service_account_email" {
  description = "Service account the Cloud Run service runs as. Grant additional roles here for optional integrations."
  value       = google_service_account.forwarder.email
}

output "bigquery_datasets" {
  description = "Per-region fully qualified BigQuery datasets holding the per-model request-response tables (one dataset per ``regions`` entry)."
  value = {
    for region, ds in google_bigquery_dataset.reqresp_logs :
    region => "${var.project_id}.${ds.dataset_id}"
  }
}

output "bigquery_table_ids" {
  description = "One BigQuery table per (region, publisher model). Keys are ``<region_slug>__<publisher>_<model>``."
  value = {
    for slug, table in google_bigquery_table.per_model :
    slug => "${var.project_id}.${google_bigquery_dataset.reqresp_logs[local.google_observed_models[slug].region].dataset_id}.${table.table_id}"
  }
}

output "checkpoint_collection" {
  description = "Firestore collection under which the per-source checkpoint documents live (``checkpoint_bq_<region_slug>`` per region for the BQ path, ``checkpoint_audit_only`` shared for the audit-only path)."
  value       = var.checkpoint_collection
}

output "push_token_secret_id" {
  description = "Secret Manager secret ID that stores the SlashID push token."
  value       = google_secret_manager_secret.push_token.secret_id
}
