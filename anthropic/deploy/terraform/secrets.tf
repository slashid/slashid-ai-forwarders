# Three secrets, each created only when the capability that needs it is
# configured. The push token is unconditional: both capabilities push.

resource "google_secret_manager_secret" "push_token" {
  secret_id = "${var.secret_prefix}_push_token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "push_token" {
  secret      = google_secret_manager_secret.push_token.id
  secret_data = var.slashid_push_token

  # Read once, when the version is created, so an apply cannot rotate the
  # secret. Rotate with ``terraform apply -replace`` (see the README).
  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret" "signing_secret" {
  count     = local.signing_secret_set ? 1 : 0
  secret_id = "${var.secret_prefix}_signing_secret"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "signing_secret" {
  count       = local.signing_secret_set ? 1 : 0
  secret      = google_secret_manager_secret.signing_secret[0].id
  secret_data = var.hook_signing_secret

  # Read once, when the version is created, so an apply cannot rotate the
  # secret. Rotate with ``terraform apply -replace`` (see the README).
  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret" "compliance_key" {
  count     = local.compliance_enabled ? 1 : 0
  secret_id = "${var.secret_prefix}_compliance_key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "compliance_key" {
  count       = local.compliance_enabled ? 1 : 0
  secret      = google_secret_manager_secret.compliance_key[0].id
  secret_data = var.compliance_key

  # Read once, when the version is created, so an apply cannot rotate the
  # secret. Rotate with ``terraform apply -replace`` (see the README).
  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret" "ghcr_token" {
  count     = var.ghcr_username == "" ? 0 : 1
  secret_id = "${var.secret_prefix}_ghcr_token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "ghcr_token" {
  count       = var.ghcr_username == "" ? 0 : 1
  secret      = google_secret_manager_secret.ghcr_token[0].id
  secret_data = var.ghcr_token

  # Read once, when the version is created, so an apply cannot rotate the
  # secret. Rotate with ``terraform apply -replace`` (see the README).
  lifecycle {
    ignore_changes = [secret_data]
  }
}
