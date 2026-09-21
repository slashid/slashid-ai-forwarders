# The runtime service account. Every grant is the minimum one tick or
# one frame needs: Firestore for the pending store and the checkpoints,
# Secret Manager for the three secrets, Artifact Registry for the image.
# It holds no BigQuery, logging or storage role — this service reads
# nothing in the customer's project beyond its own state.

resource "google_service_account" "receiver" {
  account_id   = var.service_account_id
  display_name = "SlashID Anthropic forwarder"
  description  = "Runs the Cloud Run service that receives Inference hooks and polls the Compliance API."
}

resource "google_project_iam_member" "datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.receiver.email}"
}

resource "google_secret_manager_secret_iam_member" "push_token" {
  secret_id = google_secret_manager_secret.push_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

resource "google_secret_manager_secret_iam_member" "signing_secret" {
  count     = local.signing_secret_set ? 1 : 0
  secret_id = google_secret_manager_secret.signing_secret[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

resource "google_secret_manager_secret_iam_member" "compliance_key" {
  count     = local.compliance_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.compliance_key[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

# Cloud Run pulls with its own service agent, which already holds
# roles/run.serviceAgent in-project; this grant matters only if the
# registry ever moves to another project. Kept so that move needs no
# IAM change.
resource "google_artifact_registry_repository_iam_member" "pull" {
  location   = google_artifact_registry_repository.ghcr.location
  repository = google_artifact_registry_repository.ghcr.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.receiver.email}"
}

# The scheduler's token is accepted because of this one grant.
resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.receiver.location
  name     = google_cloud_run_v2_service.receiver.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# Anthropic calls the hook URL unauthenticated — the Standard Webhooks
# signature is the authentication, and the receiver answers 401 without
# a valid one. Granted only when the hook is enabled, so a
# compliance-only deployment has no public surface at all.
#
# Note what it also reaches: ``POST /tick`` on the same service, because
# Cloud Run cannot scope an invoker to one path. The route checks the
# caller's token itself for exactly that reason, and this binding does
# not weaken that check. Even past it, a tick does no more than the
# scheduled one — it takes the same lease, honours the same checkpoints
# and emits only what the next tick would have emitted — which is worth
# knowing before pointing a rate limiter at this service.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = local.hook_enabled ? 1 : 0
  project  = var.project_id
  location = google_cloud_run_v2_service.receiver.location
  name     = google_cloud_run_v2_service.receiver.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
