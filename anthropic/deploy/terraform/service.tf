# One service, two routes.
#
# ``max_instance_request_concurrency`` bounds how many requests share an
# instance. It does NOT serialize ticks: a second concurrent POST /tick
# is served by a second instance, and Cloud Scheduler's own retry does
# not suppress an overlap either. Overlapping ticks are the steady state
# under load, and the Firestore lease the tick takes before doing any
# work is the only thing that makes them safe. Do not "fix" overlap by
# lowering this value.

resource "google_cloud_run_v2_service" "receiver" {
  name     = var.service_name
  location = var.region

  # A hook deployment must be reachable from Anthropic's network. A
  # compliance-only one needs no public endpoint and no certificate:
  # Cloud Scheduler in the same project counts as internal traffic.
  ingress = local.hook_enabled ? "INGRESS_TRAFFIC_ALL" : "INGRESS_TRAFFIC_INTERNAL_ONLY"

  # Provider 6 defaults this to true, which makes ``terraform destroy``
  # fail until it is flipped. The durable state is in Firestore, which
  # this module abandons rather than deletes.
  deletion_protection = false

  template {
    service_account = google_service_account.receiver.email
    timeout         = "${var.service_timeout_seconds}s"

    max_instance_request_concurrency = 20

    scaling {
      # Only the hook needs a warm instance: a cold start inside
      # Anthropic's verdict timeout risks a webhook failure, and enough
      # of those trip its circuit breaker. A scheduled reader can wait.
      min_instance_count = local.hook_enabled ? var.min_instances : 0
      max_instance_count = var.max_instances
    }

    containers {
      image = local.image

      resources {
        limits = {
          cpu    = "1"
          memory = var.memory
        }
        # The push runs after the response has gone out, so CPU stays
        # allocated between requests.
        cpu_idle = false
      }

      env {
        name  = "SLASHID_ENDPOINT"
        value = var.slashid_endpoint
      }
      env {
        name  = "SLASHID_PLATFORM"
        value = "gcp"
      }
      env {
        name  = "SLASHID_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "SLASHID_DATABASE"
        value = var.database
      }
      env {
        name  = "SLASHID_PENDING_COLLECTION"
        value = var.pending_collection
      }
      # Capture is off unless a bucket is named. See the variable for why
      # it should stay that way outside a test tenant.
      dynamic "env" {
        for_each = var.capture_bucket == "" ? [] : [1]
        content {
          name  = "SLASHID_CAPTURE_BUCKET"
          value = var.capture_bucket
        }
      }
      dynamic "env" {
        for_each = var.capture_deny_marker == "" ? [] : [1]
        content {
          name  = "SLASHID_CAPTURE_DENY_MARKER"
          value = var.capture_deny_marker
        }
      }
      env {
        name  = "SLASHID_CHECKPOINT_COLLECTION"
        value = var.checkpoint_collection
      }
      # The soft join's unanimity window. Measured: at 15s three of the
      # corpus's attachment rounds resolve to exactly one candidate; at 60s
      # one of them gains a second and correctly abstains. So it is an
      # operational knob, not a constant.
      env {
        name  = "SLASHID_SOFT_JOIN_WINDOW_SECONDS"
        value = tostring(var.soft_join_window_seconds)
      }
      # ``/tick`` verifies the caller's bearer token against this address.
      # Without it every scheduled tick 401s and the readers and the flush
      # never run — silently, because a 401 is a fine-looking response. The
      # audience is deliberately not set here: it would have to be the
      # service's own URI, which is an attribute of the resource whose
      # environment would carry it, and that is a cycle. Email plus a
      # Google signature is the authorization; where ingress is internal,
      # Cloud Run checks the audience itself.
      env {
        name  = "SLASHID_TICK_PRINCIPAL"
        value = google_service_account.scheduler.email
      }
      env {
        name  = "SLASHID_JOIN_WAIT_SECONDS"
        value = tostring(var.join_wait_seconds)
      }
      env {
        name  = "SLASHID_TOMBSTONE_TTL_SECONDS"
        value = tostring(var.tombstone_ttl_seconds)
      }
      env {
        name  = "SLASHID_MAX_FLUSHES_PER_TICK"
        value = tostring(var.max_flushes_per_tick)
      }
      env {
        name  = "SLASHID_TICK_INTERVAL_SECONDS"
        value = tostring(var.tick_interval_seconds)
      }
      env {
        name  = "SLASHID_POLL_LAG_SECONDS"
        value = tostring(var.poll_lag_seconds)
      }
      env {
        name  = "SLASHID_MAX_SESSIONS_PER_TICK"
        value = tostring(var.max_sessions_per_tick)
      }
      env {
        name  = "SLASHID_ORGANIZATION_UUID"
        value = var.organization_uuid
      }
      env {
        name  = "SLASHID_ATTACHMENT_HASHING"
        value = var.attachment_hashing
      }
      env {
        name  = "SLASHID_MAX_ATTACHMENT_FETCH_BYTES"
        value = tostring(var.max_attachment_fetch_bytes)
      }
      env {
        name  = "SLASHID_PREFLIGHT_ENABLED"
        value = tostring(var.preflight_enabled)
      }
      env {
        name  = "SLASHID_VERDICT_FAIL_MODE"
        value = var.verdict_fail_mode
      }
      env {
        name  = "SLASHID_SHADOW_MODE"
        value = tostring(var.shadow_mode)
      }
      env {
        name  = "SLASHID_VERDICT_BUDGET_MS"
        value = tostring(var.verdict_budget_ms)
      }
      env {
        name  = "SLASHID_PUSH_BUDGET_MS"
        value = tostring(var.push_budget_ms)
      }
      env {
        name  = "SLASHID_MAX_BODY_BYTES"
        value = tostring(var.max_body_bytes)
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
        name  = "LOG_LEVEL"
        value = var.log_level
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

      dynamic "env" {
        for_each = local.signing_secret_set ? [1] : []
        content {
          name = "SLASHID_HOOK_SIGNING_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.signing_secret[0].secret_id
              version = "latest"
            }
          }
        }
      }

      dynamic "env" {
        for_each = local.compliance_enabled ? [1] : []
        content {
          name = "SLASHID_COMPLIANCE_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.compliance_key[0].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.tombstone_ttl_seconds > var.join_wait_seconds + var.poll_lag_seconds + var.tick_interval_seconds
      error_message = "tombstone_ttl_seconds must exceed join_wait_seconds + poll_lag_seconds + tick_interval_seconds: a reader arriving after its own tombstone expired re-emits the invocation. The service asserts the same inequality at startup."
    }

    precondition {
      condition     = var.tick_attempt_deadline_seconds <= var.service_timeout_seconds
      error_message = "tick_attempt_deadline_seconds must not exceed service_timeout_seconds: Cloud Scheduler would give up on a tick Cloud Run is still running."
    }

    precondition {
      condition     = !(local.compliance_enabled && var.organization_uuid == "")
      error_message = "organization_uuid is required with compliance_key: the key can read every linked organization, so the readers filter to one."
    }
  }

  # Every secret this revision mounts, version and accessor grant alike.
  # The ``env`` blocks above reference the *secret* and ask for
  # ``version = "latest"``, which creates no dependency on the version
  # resource and none on the IAM member either — so without these the
  # graph is free to start the revision before the signing secret has a
  # value or before the runtime account may read the compliance key, and
  # the revision fails with a Secret Manager access error. The push token
  # was already guarded; the other two were not, and they are the two a
  # first apply is most likely to lose the race on.
  #
  # Both conditional pairs are listed without an index, which is how
  # ``depends_on`` refers to every instance of a counted resource — zero
  # instances is an empty dependency, not an error.
  depends_on = [
    google_secret_manager_secret_version.push_token,
    google_secret_manager_secret_iam_member.push_token,
    google_secret_manager_secret_version.signing_secret,
    google_secret_manager_secret_iam_member.signing_secret,
    google_secret_manager_secret_version.compliance_key,
    google_secret_manager_secret_iam_member.compliance_key,
    google_artifact_registry_repository.ghcr,
    google_project_iam_member.datastore_user,
  ]
}
