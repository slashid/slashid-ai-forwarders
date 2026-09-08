# Firestore Native-mode database — singleton per project until
# multi-database GA (planned but not shipped as of provider v6). The
# checkpoint document itself is created on the first ``FirestoreCheckpointStore.save`` call by the Cloud Function — no
# TF resource creates it up-front.
#
# ``create_firestore_database`` defaults to true because most customers
# won't have a Firestore in their project. Flip to false to reuse an
# existing one (module still writes to the same collection/document).

resource "google_firestore_database" "default" {
  count = var.create_firestore_database ? 1 : 0

  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Firestore database is not straightforward to recreate — protect
  # against unintended destroys.
  deletion_policy = "ABANDON"

  depends_on = [google_project_service.required]
}
