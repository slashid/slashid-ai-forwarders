# SlashID push token — stored in Secret Manager, mounted into the
# Cloud Function as an env var (see function.tf ``secret_environment_variables``).

resource "google_secret_manager_secret" "push_token" {
  secret_id = var.secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "push_token" {
  secret      = google_secret_manager_secret.push_token.id
  secret_data = var.slashid_push_token

  # The value is read once, when the version is created, so an apply cannot
  # rotate the token. Rotate with ``terraform apply -replace``.
  lifecycle {
    ignore_changes = [secret_data]
  }
}
