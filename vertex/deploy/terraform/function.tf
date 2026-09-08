# Cloud Function 2nd gen + staging bucket + Cloud Scheduler + Pub/Sub trigger.
#
# Flow:
#   Cloud Scheduler cron → Pub/Sub trigger topic → Cloud Function 2nd gen
#
# The function runs one polling tick per scheduler fire. Function
# concurrency is pinned to 1 so checkpoint reads/writes never race.

# --- Release-artifact staging ----------------------------------------------
#
# One bucket per project, one object per release_version — object name
# is templated by the tag (see ``google_storage_bucket_object.source``
# below) so multiple releases coexist for quick rollback. Old objects
# auto-expire after 90 days.
#
# We do NOT enable bucket versioning: each release has a distinct
# object name so there's no "same-object noncurrent-version" chain to
# prune. Rolling back is ``terraform apply -var release_version=<prior>``
# — TF re-downloads and re-uploads the prior zip under its distinct
# name.

resource "google_storage_bucket" "release" {
  name                        = local.release_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      # 90 days covers the typical rollback horizon (last quarter's
      # release) without accumulating years of dead objects.
      age = 90
    }
  }

  depends_on = [google_project_service.required]
}

# Fetch the release zip from GitHub Releases at plan time. If the URL
# 404s (release not yet published), plan fails — that's the intended
# behaviour, prevents deploying a phantom version.
# Download the release zip via ``gh release download`` and stage it
# under the module.
#
# Why gh instead of curl / data.http:
#   - The repo is private, so every fetch needs GitHub auth. ``gh``
#     already carries the caller's auth (``gh auth login`` or
#     ``GH_TOKEN``), so we don't have to plumb a token variable
#     through the module.
#   - The plain ``https://github.com/.../releases/download/{tag}/{name}``
#     URL 404-caches aggressively when a release is deleted-and-
#     recreated under the same tag+filename (our workflow does that
#     on every re-tag). ``gh release download`` resolves the current
#     asset ID via the API and downloads that direct pointer instead,
#     sidestepping the cached rewrite.
#
# ``local-exec`` provisioners can't discover filesystem state, and
# the ``.release.zip`` we write lives under ``.terraform/modules/``
# which gets wiped whenever a customer runs ``rm -rf .terraform`` or
# ``terraform init -upgrade`` to refresh a re-tagged module version.
# TF's own state would still mark the null_resource as created and
# skip the provisioner — so trigger on wall-clock time to re-fire on
# every apply. ~20MB from GH Releases is cheap.
# ``google_storage_bucket_object`` below detects content-level
# changes via ``source_md5hash``, so identical bytes don't get
# re-uploaded.
resource "null_resource" "download_release_zip" {
  triggers = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    command = <<-EOT
      gh release download "${var.release_version}" \
        --repo "${var.release_repo}" \
        --pattern "${local.release_zip_filename}" \
        --output "${path.module}/.release.zip" \
        --clobber
    EOT
  }
}

resource "google_storage_bucket_object" "source" {
  name         = local.release_zip_filename
  bucket       = google_storage_bucket.release.name
  source       = "${path.module}/.release.zip"
  content_type = "application/zip"

  depends_on = [null_resource.download_release_zip]
}

# --- Pub/Sub trigger + Scheduler ------------------------------------------

resource "google_pubsub_topic" "trigger" {
  name = var.trigger_topic_name

  depends_on = [google_project_service.required]
}

resource "google_cloud_scheduler_job" "poll" {
  name        = var.scheduler_name
  schedule    = var.poll_schedule
  time_zone   = "UTC"
  region      = var.region
  description = "Fires the SlashID Vertex forwarder on ``${var.poll_schedule}`` (UTC)."

  pubsub_target {
    topic_name = google_pubsub_topic.trigger.id
    # Body is ignored by the function — the tick IS the trigger. Send
    # a minimal payload to satisfy the API contract.
    data = base64encode(jsonencode({ trigger = "poll" }))
  }

  depends_on = [google_project_service.required]
}

# --- Cloud Function 2nd gen ------------------------------------------------

resource "google_cloudfunctions2_function" "forwarder" {
  name        = var.function_name
  location    = var.region
  description = "SlashID Vertex forwarder — polls BigQuery request-response logs and pushes to SlashID NHI."

  build_config {
    runtime     = "python313"
    entry_point = "handler"
    source {
      storage_source {
        bucket = google_storage_bucket.release.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    max_instance_count               = 1
    min_instance_count               = 0
    available_memory                 = "512Mi"
    timeout_seconds                  = 540
    max_instance_request_concurrency = 1
    service_account_email            = google_service_account.forwarder.email
    ingress_settings                 = "ALLOW_INTERNAL_ONLY"

    environment_variables = {
      LOG_LEVEL                               = var.log_level
      SLASHID_ENDPOINT                        = var.slashid_endpoint
      SLASHID_GCP_PROJECT_ID                  = var.project_id
      SLASHID_GCP_REGION                      = var.region
      SLASHID_BQ_DATASET                      = var.bq_dataset_id
      SLASHID_FIRESTORE_DATABASE              = var.firestore_database
      SLASHID_FIRESTORE_CHECKPOINT_COLLECTION = var.firestore_checkpoint_collection
      SLASHID_FIRESTORE_CHECKPOINT_DOCUMENT   = var.firestore_checkpoint_document
      SLASHID_INCLUDE_RAW_CONTENT             = tostring(var.include_raw_content)
      SLASHID_MAX_CONTENT_SIZE                = tostring(var.max_content_size)
      SLASHID_MAX_ROWS_PER_TICK               = tostring(var.max_rows_per_tick)
      SLASHID_REQUEST_TIMEOUT_SECONDS         = tostring(var.request_timeout_seconds)
    }

    secret_environment_variables {
      key        = "SLASHID_PUSH_TOKEN"
      project_id = var.project_id
      secret     = google_secret_manager_secret.push_token.secret_id
      version    = "latest"
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = google_pubsub_topic.trigger.id
    retry_policy   = "RETRY_POLICY_DO_NOT_RETRY"

    service_account_email = google_service_account.forwarder.email
  }

  depends_on = [
    google_project_iam_member.bigquery_data_viewer,
    google_project_iam_member.bigquery_job_user,
    google_project_iam_member.datastore_user,
    google_project_iam_member.secret_accessor,
    google_secret_manager_secret_version.push_token,
  ]
}
