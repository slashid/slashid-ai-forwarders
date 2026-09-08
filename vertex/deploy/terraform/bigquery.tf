# BigQuery dataset + per-model tables + setPublisherModelConfig calls.
#
# ``setPublisherModelConfig`` has no native TF resource in provider v6
# (checked 2026-09-08) and gcloud lacks a stable subcommand for it too
# — the REST API lives on ``v1beta1`` only. We drive it via
# ``null_resource`` + ``local-exec`` curl against the REST endpoint.
# Auth token comes from ``gcloud auth print-access-token`` (whichever
# account is active); ADC alone doesn't help here because curl needs
# an actual bearer token to send.
#
# Swap to a native TF resource as soon as the provider adds one.
#
# The per-table schema mirrors what Vertex request-response logging
# actually writes — pinned here so BQ table creation happens before the
# publisher-model config points at it, and so the BqEventSource query
# projects the columns it expects.

resource "google_bigquery_dataset" "reqresp_logs" {
  dataset_id                 = var.bq_dataset_id
  location                   = var.region
  delete_contents_on_destroy = false
  description                = "SlashID Vertex forwarder — per-model Vertex request-response logging tables."

  depends_on = [google_project_service.required]
}

resource "google_bigquery_table" "per_model" {
  for_each   = local.observed_models
  dataset_id = google_bigquery_dataset.reqresp_logs.dataset_id
  table_id   = "slashid_vertex_reqresp_${each.key}"

  # HOUR-on-logging_time matches Vertex's own default when the API
  # auto-creates a destination table — pinning here keeps every
  # per-model table partitioning-compatible, which is a hard
  # requirement for the wildcard-table read in ``BqEventSource``.
  # Also lets BQ prune the polling query to the recent partitions.
  #
  # 24h partition TTL: the forwarder consumes each row exactly once
  # (checkpoint advances past it) and doesn't need long-term
  # storage. 24h leaves a reprocess window if the forwarder needs
  # to be rewound (checkpoint reset) after an outage; older data is
  # effectively dead weight + storage cost.
  time_partitioning {
    type          = "HOUR"
    field         = "logging_time"
    expiration_ms = 86400000
  }

  # Schema mirrors Vertex request-response logging output. Only the
  # columns BqEventSource projects (``request_id``, ``logging_time``,
  # ``model``, ``full_request``, ``full_response``) are strictly needed
  # — the rest are captured for future correlation phases.
  schema = jsonencode([
    { name = "endpoint", type = "STRING", mode = "NULLABLE" },
    { name = "deployed_model_id", type = "STRING", mode = "NULLABLE" },
    { name = "logging_time", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "request_id", type = "NUMERIC", mode = "REQUIRED" },
    { name = "request_payload", type = "STRING", mode = "REPEATED" },
    { name = "response_payload", type = "STRING", mode = "REPEATED" },
    { name = "model", type = "STRING", mode = "NULLABLE" },
    { name = "model_version", type = "STRING", mode = "NULLABLE" },
    { name = "api_method", type = "STRING", mode = "NULLABLE" },
    { name = "full_request", type = "JSON", mode = "NULLABLE" },
    { name = "full_response", type = "JSON", mode = "NULLABLE" },
    { name = "metadata", type = "JSON", mode = "NULLABLE" },
    { name = "otel_log", type = "JSON", mode = "NULLABLE" },
  ])

  # No deletion protection — the tables are cheap to recreate and the
  # module is intended to be reversible.
  deletion_protection = false
}

# Enable request-response logging on each configured publisher model.
# ``publisherModels.setPublisherModelConfig`` (v1beta1) is the only
# way to route request-response logs to a BQ destination today — no
# native TF resource, no stable gcloud subcommand. curl carries the
# call; the machine running ``terraform apply`` needs gcloud
# authenticated to a principal with ``aiplatform.endpoints.setPublisherModelConfig``
# on the project (``roles/aiplatform.admin`` includes it;
# ``roles/owner`` does too).
#
# ``triggers`` re-runs the exec when the destination table or model
# list changes. Propagation takes ~10 min for first-time enablement.
#
# Cleanup: dropping a model from ``observed_models`` destroys the
# ``null_resource``, firing the ``when = destroy`` provisioner below
# to disable logging and zero the sampling rate. The full config
# isn't unset — the API has no clear "clear" verb — but
# ``enabled: false`` stops writes reliably.
resource "null_resource" "publisher_model_logging" {
  for_each = local.observed_models

  # ``self`` inside a destroy-time provisioner only sees ``triggers`` —
  # copy every field the destroy command references so it works after
  # the resource is scheduled for destruction. Bumping ``config_schema``
  # forces re-run when the API request shape changes across module
  # versions (we've hit this once already going from gcloud → curl).
  triggers = {
    model         = each.value.full
    publisher     = each.value.publisher
    model_id      = each.value.model
    table         = google_bigquery_table.per_model[each.key].id
    table_slug    = each.key
    dataset       = google_bigquery_dataset.reqresp_logs.dataset_id
    project       = var.project_id
    region        = var.region
    sampling_rate = "1.0"
    config_schema = "curl-v1beta1"
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      BODY='{"publisherModelConfig":{"loggingConfig":{"enabled":true,"samplingRate":1.0,"bigqueryDestination":{"outputUri":"bq://${var.project_id}.${var.bq_dataset_id}.slashid_vertex_reqresp_${each.key}"},"enableOtelLogging":true}}}'
      curl -sS --fail-with-body -X POST \
        -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        -H "Content-Type: application/json" \
        --data "$${BODY}" \
        "https://${var.region}-aiplatform.googleapis.com/v1beta1/projects/${var.project_id}/locations/${var.region}/publishers/${each.value.publisher}/models/${each.value.model}:setPublisherModelConfig"
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      curl -sS --fail-with-body -X POST \
        -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        -H "Content-Type: application/json" \
        --data '{"publisherModelConfig":{"loggingConfig":{"enabled":false,"samplingRate":0}}}' \
        "https://${self.triggers.region}-aiplatform.googleapis.com/v1beta1/projects/${self.triggers.project}/locations/${self.triggers.region}/publishers/${self.triggers.publisher}/models/${self.triggers.model_id}:setPublisherModelConfig" \
      || true
    EOT
  }

  depends_on = [google_bigquery_table.per_model]
}
