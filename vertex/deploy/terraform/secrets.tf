# SlashID push token — stored in Secret Manager, mounted into the
# Cloud Run service as an env var (see service.tf).

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

# The token the registry's upstream credential presents to ghcr.io. Only
# while the package is private, hence the count. Read once, like the push
# token, so an apply cannot rotate it.
resource "google_secret_manager_secret" "ghcr_token" {
  count     = var.ghcr_username == "" ? 0 : 1
  secret_id = var.ghcr_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "ghcr_token" {
  count       = var.ghcr_username == "" ? 0 : 1
  secret      = google_secret_manager_secret.ghcr_token[0].id
  secret_data = var.ghcr_token

  lifecycle {
    ignore_changes = [secret_data]
  }
}
