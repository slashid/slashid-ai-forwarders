# Firestore Native-mode database.
#
# Multi-database Firestore is GA — we create a NAMED database
# (``slashid-vertex`` by default) instead of touching the project's
# ``(default)`` database. Keeps the forwarder isolated from any other
# Firestore usage in the customer's project.
#
# ``create_firestore_database`` defaults to true. Set to false if the
# named database already exists (e.g. after ``terraform destroy`` +
# re-apply — Firestore databases are hard to fully delete, so many
# customers will leave them around and reuse across cycles).
#
# The checkpoint document itself is created on the first
# ``FirestoreCheckpointStore.save`` call by the Cloud Function — no
# TF resource creates it up-front.

resource "google_firestore_database" "vertex" {
  count = var.create_firestore_database ? 1 : 0

  project     = var.project_id
  name        = var.firestore_database
  location_id = local.deployment_region
  type        = "FIRESTORE_NATIVE"

  # Firestore databases can't be undeleted; protect against
  # unintended destroys.
  deletion_policy = "ABANDON"

  depends_on = [google_project_service.required]
}
