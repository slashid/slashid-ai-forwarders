# Cloud Run pulls images from Artifact Registry only, so a REMOTE
# repository proxies ghcr.io, where the release workflow publishes.

resource "google_artifact_registry_repository" "ghcr" {
  location      = local.deployment_region
  repository_id = var.registry_repository_id
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"

  remote_repository_config {
    description = "Proxy for ghcr.io"

    docker_repository {
      custom_repository {
        uri = "https://ghcr.io"
      }
    }

    dynamic "upstream_credentials" {
      for_each = var.ghcr_username == "" ? [] : [1]
      content {
        username_password_credentials {
          username                = var.ghcr_username
          password_secret_version = google_secret_manager_secret_version.ghcr_token[0].name
        }
      }
    }

    # The API validates the upstream credential at create time, before the
    # service agent's read grant below has necessarily propagated.
    disable_upstream_validation = true
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.registry_reads_ghcr_token,
  ]
}

# The registry's own service agent reads the upstream token — not the
# runtime service account.
resource "google_secret_manager_secret_iam_member" "registry_reads_ghcr_token" {
  count     = var.ghcr_username == "" ? 0 : 1
  secret_id = google_secret_manager_secret.ghcr_token[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"

  depends_on = [google_project_service.required]
}
