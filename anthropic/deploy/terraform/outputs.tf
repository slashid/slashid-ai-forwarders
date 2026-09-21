output "hook_url" {
  description = "Configure this as the Inference hooks endpoint in claude.ai. Empty when the hook is disabled."
  value       = local.hook_enabled ? "${google_cloud_run_v2_service.receiver.uri}${var.hook_path}" : ""
}

output "service_uri" {
  description = "Cloud Run service base URL. ``POST /tick`` under it is what Cloud Scheduler calls."
  value       = google_cloud_run_v2_service.receiver.uri
}

output "service_account_email" {
  description = "Service account the receiver runs as."
  value       = google_service_account.receiver.email
}

output "scheduler_service_account_email" {
  description = "Service account Cloud Scheduler mints its OIDC token as."
  value       = google_service_account.scheduler.email
}

output "tick_schedule" {
  description = "Unix-cron schedule derived from tick_interval_seconds."
  value       = local.tick_schedule
}

output "image" {
  description = "Image the service runs, resolved through the Artifact Registry proxy."
  value       = local.image
}

output "firestore_database" {
  description = "Named Firestore database holding the pending records and the reader checkpoints."
  value       = var.firestore_database
}

output "capabilities" {
  description = "Which halves this deployment runs."
  value = {
    hook       = local.hook_enabled
    compliance = local.compliance_enabled
  }
}
