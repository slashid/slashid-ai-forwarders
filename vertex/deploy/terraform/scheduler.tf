# Cloud Scheduler fires the tick: one POST to /tick per ``poll_schedule``.
#
# OIDC, not a shared secret: the token is minted for a service account
# whose only privilege is invoking this service.
#
# ``retry_count = 0``. A retried tick does not resume the one that timed
# out and does not exclude it either; the next cron fire picks the work
# up from the checkpoints, and the tick's Firestore lease is what keeps
# two concurrent runs from reading the same window.

resource "google_service_account" "scheduler" {
  account_id   = var.scheduler_service_account_id
  display_name = "SlashID Vertex tick scheduler"
  description  = "Mints the OIDC token Cloud Scheduler presents to POST /tick."
}

# The scheduler's token is accepted because of this one grant.
resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.forwarder.location
  name     = google_cloud_run_v2_service.forwarder.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "poll" {
  name             = var.scheduler_name
  schedule         = var.poll_schedule
  time_zone        = "UTC"
  region           = local.deployment_region
  attempt_deadline = "${var.tick_attempt_deadline_seconds}s"
  description      = "Fires the SlashID Vertex forwarder on ``${var.poll_schedule}`` (UTC)."

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.forwarder.uri}/tick"
    body        = base64encode("{}")

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.forwarder.uri
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_service_iam_member.scheduler_invoker,
  ]
}
