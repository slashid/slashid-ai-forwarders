# Firestore Native-mode, in a NAMED database (``slashid-anthropic``)
# rather than the project's ``(default)`` — the same isolation
# ``vertex/`` takes for its own ``slashid-vertex``.
#
# Two collections live in it: the pending records (below) and the
# compliance readers' checkpoint documents, which are created on the
# first save and need no resource here.

resource "google_firestore_database" "pending" {
  count = var.create_database ? 1 : 0

  project     = var.project_id
  name        = var.database
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Firestore databases cannot be undeleted.
  deletion_policy = "ABANDON"

  depends_on = [google_project_service.required]
}

# The deadline sweep runs
#
#   where(tombstoned_at == None).where(next_attempt_at <= now)
#     .order_by(next_attempt_at).limit(max_flushes_per_tick)
#
# — an equality plus an inequality on a second field, which Firestore
# serves only from a composite index. Without it the query fails at
# runtime with FAILED_PRECONDITION and no record is ever flushed.
#
# ``next_attempt_at`` folds the deadline, the claim lease and the retry
# backoff into one field, which is why one inequality is enough and this
# index has two terms rather than three.
resource "google_firestore_index" "due" {
  project     = var.project_id
  database    = var.database
  collection  = var.pending_collection
  query_scope = "COLLECTION"

  fields {
    field_path = "tombstoned_at"
    order      = "ASCENDING"
  }

  fields {
    field_path = "next_attempt_at"
    order      = "ASCENDING"
  }

  depends_on = [google_firestore_database.pending]
}

# TTL policy. Firestore deletes a document once the nominated field's
# timestamp is in the PAST, and offers no duration of its own — so the
# field holds the expiry instant the store computed
# (``tombstoned_at + SLASHID_TOMBSTONE_TTL_SECONDS``), never the moment
# of tombstoning. Keying it on ``tombstoned_at`` would ask Firestore to
# delete each tombstone the moment it was written, leaving a late reader
# free to re-emit an invocation that was already pushed.
#
# A live record never carries this field, so the policy cannot reach one.
# That matters more than the duplicate does: expiring by creation time —
# the obvious implementation under a 7200 s default — would delete a
# record that had been failing to push for two hours before it was ever
# emitted, which is precisely the loss the store exists to prevent.
#
# ``index_config {}`` clears the single-field indexes on the TTL field;
# nothing queries it and Firestore recommends the exemption.
resource "google_firestore_field" "tombstone_ttl" {
  project    = var.project_id
  database   = var.database
  collection = var.pending_collection
  field      = "tombstone_expires_at"

  ttl_config {}

  index_config {}

  depends_on = [google_firestore_database.pending]
}
