# One Cloud Run service with one route: Cloud Scheduler posts to /tick.
#
# ``max_instance_request_concurrency`` stays above one so that an
# overlapping tick is answered at once by the Firestore lease instead of
# queueing behind the running one. It does NOT serialize ticks, and
# lowering it would not make them safe: the lease is what does.

resource "google_cloud_run_v2_service" "forwarder" {
  name     = var.service_name
  location = local.deployment_region

  # Only Cloud Scheduler calls this service, and in the same project that
  # counts as internal traffic.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  # Provider 6 defaults this to true, which makes ``terraform destroy``
  # fail until it is flipped. The durable state is in Firestore and
  # BigQuery, which this module does not delete with the service.
  deletion_protection = false

  template {
    service_account = google_service_account.forwarder.email
    timeout         = "${var.service_timeout_seconds}s"

    max_instance_request_concurrency = 20

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = local.image

      resources {
        limits = {
          cpu    = "1"
          memory = var.memory
        }
      }

      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }
      env {
        name  = "SLASHID_ENDPOINT"
        value = var.slashid_endpoint
      }
      env {
        name  = "SLASHID_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "SLASHID_GCP_REGIONS"
        value = jsonencode(var.regions)
      }
      env {
        name  = "SLASHID_BQ_DATASET_PREFIX"
        value = var.bq_dataset_prefix
      }
      env {
        name  = "SLASHID_DATABASE"
        value = var.database
      }
      env {
        name  = "SLASHID_CHECKPOINT_COLLECTION"
        value = var.checkpoint_collection
      }
      env {
        name  = "SLASHID_AUDIT_BUFFER_SECONDS"
        value = tostring(var.audit_buffer_seconds)
      }
      env {
        name  = "SLASHID_INCLUDE_RAW_CONTENT"
        value = tostring(var.include_raw_content)
      }
      env {
        name  = "SLASHID_MAX_CONTENT_SIZE"
        value = tostring(var.max_content_size)
      }
      env {
        name  = "SLASHID_MAX_ROWS_PER_TICK"
        value = tostring(var.max_rows_per_tick)
      }
      env {
        name  = "SLASHID_REQUEST_TIMEOUT_SECONDS"
        value = tostring(var.request_timeout_seconds)
      }
      env {
        name  = "SLASHID_AUDIT_OBSERVED_MODELS"
        value = jsonencode(local.effective_observed_models)
      }
      # The audience is left unset: it is this service's own URI, which the
      # Terraform that sets this environment cannot name without a cycle.
      # The account still authorizes the tick, and Cloud Run checks the
      # audience itself.
      env {
        name  = "SLASHID_TICK_PRINCIPAL"
        value = google_service_account.scheduler.email
      }

      env {
        name = "SLASHID_PUSH_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.push_token.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.tick_attempt_deadline_seconds <= var.service_timeout_seconds
      error_message = "tick_attempt_deadline_seconds must not exceed service_timeout_seconds: Cloud Scheduler would give up on a tick Cloud Run is still running."
    }
  }

  # The ``env`` secret reference asks for ``version = "latest"``, which
  # creates no dependency on the version resource or on the grants, so
  # without these the graph may start a revision before the token has a
  # value or before the runtime account may read it.
  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_version.push_token,
    google_project_iam_member.secret_accessor,
    google_project_iam_member.bigquery_data_viewer,
    google_project_iam_member.bigquery_job_user,
    google_project_iam_member.datastore_user,
    google_project_iam_member.logging_viewer,
    google_artifact_registry_repository.ghcr,
  ]
}
