# BigQuery dataset + per-model tables + setPublisherModelConfig calls.
#
# ``setPublisherModelConfig`` has no native TF resource in provider v6
# (checked 2026-09-08); we drive it via ``null_resource`` +
# ``local-exec`` gcloud. Swap to native as soon as it ships.
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
  for_each   = local.logged_models
  dataset_id = google_bigquery_dataset.reqresp_logs.dataset_id
  table_id   = "slashid_vertex_reqresp_${each.key}"

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
# gcloud does the setPublisherModelConfig REST call — the native TF
# resource doesn't exist as of provider v6.
#
# ``triggers`` re-runs the exec when the destination table or model list
# changes. Propagation takes ~10 min for first-time enablement.
#
# Cleanup: dropping a model from ``logged_publisher_models`` destroys
# the ``null_resource``, firing the ``when = destroy`` provisioner
# below to set sampling_rate = 0 — Vertex stops writing to the (now
# orphaned) BQ destination. The full config isn't unset (gcloud's
# unset command varies across versions), but sampling_rate = 0 works
# reliably against every version that supports the create-time flags.
# Customers who want to fully clear the config can do so via the Vertex
# console.
resource "null_resource" "publisher_model_logging" {
  for_each = local.logged_models

  # ``self`` inside a destroy-time provisioner only sees ``triggers`` —
  # copy every field the destroy command references so it works after
  # the resource is scheduled for destruction.
  triggers = {
    model         = each.value.full
    publisher     = each.value.publisher
    model_id      = each.value.model
    table         = google_bigquery_table.per_model[each.key].id
    dataset       = google_bigquery_dataset.reqresp_logs.dataset_id
    project       = var.project_id
    region        = var.region
    sampling_rate = "1.0"
  }

  provisioner "local-exec" {
    command = <<-EOT
      gcloud ai model-garden models set-publisher-model-config \
        --project="${var.project_id}" \
        --region="${var.region}" \
        --publisher="${each.value.publisher}" \
        --model="${each.value.model}" \
        --logging-config-bigquery-destination="bq://${var.project_id}.${var.bq_dataset_id}.slashid_vertex_reqresp_${each.key}" \
        --logging-config-sampling-rate=1.0 \
        --logging-config-enable-otel-logging
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      gcloud ai model-garden models set-publisher-model-config \
        --project="${self.triggers.project}" \
        --region="${self.triggers.region}" \
        --publisher="${self.triggers.publisher}" \
        --model="${self.triggers.model_id}" \
        --logging-config-sampling-rate=0 || true
    EOT
  }

  depends_on = [google_bigquery_table.per_model]
}
