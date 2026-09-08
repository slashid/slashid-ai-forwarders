# Log-sink exclusion for Vertex Data Access logs.
#
# Data Access audit logs on ``aiplatform.googleapis.com`` are enabled
# via the audit config in main.tf so they're ready when the identity-
# correlation phase lands. Meanwhile they cost money if they flow into
# the ``_Default`` sink and get retained. Exclude them at the sink to
# hold cost near zero without disabling the underlying capture.

resource "google_logging_project_sink" "default_exclude_vertex_data_access" {
  name        = "slashid-vertex-default-exclude"
  destination = "logging.googleapis.com/projects/${var.project_id}/locations/global/buckets/_Default"

  filter = "NOT (protoPayload.serviceName = \"aiplatform.googleapis.com\" AND protoPayload.methodName:\"generateContent\")"

  unique_writer_identity = true

  depends_on = [google_project_service.required]
}
