# Cloud Scheduler fires the tick: the compliance readers' pass and the
# deadline flush. It runs in every topology — a hook-only deployment
# still needs the flush, because the last round of a session has no
# successor frame to settle its record.
#
# OIDC, not a shared secret: the token is minted for a service account
# whose only privilege is invoking this service.
#
# ``retry_count = 0``. A retried tick does not resume the one that timed
# out and does not exclude it either; the next cron fire picks the work
# up from the store, and the tick's Firestore lease is what keeps two
# concurrent runs from pushing the same record twice.

resource "google_service_account" "scheduler" {
  account_id   = var.scheduler_service_account_id
  display_name = "SlashID Anthropic tick scheduler"
  description  = "Mints the OIDC token Cloud Scheduler presents to POST /tick."
}

resource "google_cloud_scheduler_job" "tick" {
  name             = var.scheduler_name
  region           = var.region
  schedule         = local.tick_schedule
  time_zone        = "UTC"
  attempt_deadline = "${var.tick_attempt_deadline_seconds}s"
  description      = "Drives the SlashID Anthropic readers and the deadline flush every ${var.tick_interval_seconds}s (UTC)."

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.receiver.uri}/tick"
    body        = base64encode("{}")

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.receiver.uri
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_service_iam_member.scheduler_invoker,
  ]
}
