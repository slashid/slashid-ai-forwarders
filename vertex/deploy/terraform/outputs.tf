output "function_uri" {
  description = "Cloud Function 2nd gen HTTPS URL (unused for the Pub/Sub-triggered function; useful for manual invocation during smoke). ``null`` right after ``terraform import`` — provider populates ``service_config`` on the next refresh/apply."
  value       = try(google_cloudfunctions2_function.forwarder.service_config[0].uri, null)
}

output "service_account_email" {
  description = "Service account the Cloud Function runs as. Grant additional roles here for optional integrations."
  value       = google_service_account.forwarder.email
}

output "bigquery_dataset_id" {
  description = "Fully qualified BigQuery dataset holding the per-model request-response tables."
  value       = "${var.project_id}.${google_bigquery_dataset.reqresp_logs.dataset_id}"
}

output "bigquery_table_ids" {
  description = "One BigQuery table per configured publisher model."
  value = {
    for slug, table in google_bigquery_table.per_model :
    slug => "${var.project_id}.${google_bigquery_dataset.reqresp_logs.dataset_id}.${table.table_id}"
  }
}

output "firestore_checkpoint_collection" {
  description = "Firestore collection under which the per-source checkpoint documents live (``checkpoint`` for BQ, ``checkpoint_audit_only`` for audit-only)."
  value       = var.firestore_checkpoint_collection
}

output "push_token_secret_id" {
  description = "Secret Manager secret ID that stores the SlashID push token."
  value       = google_secret_manager_secret.push_token.secret_id
}

output "trigger_topic" {
  description = "Pub/Sub topic that Cloud Scheduler publishes to and the function consumes."
  value       = google_pubsub_topic.trigger.name
}
