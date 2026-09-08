# Static Vertex Model Garden catalog.
#
# ``all_models.json`` is a maintained JSON snapshot of Vertex's
# publisher-model catalog (``publisher/model`` entries). It's read at
# plan time — no gcloud call, no external tooling, deterministic
# plans.
#
# Refresh: run ``./refresh.sh --project <GCP_PROJECT>`` to regenerate
# the file from ``gcloud ai model-garden models list``. Do this before
# cutting a new module release; scheduled workflow at
# ``.github/workflows/vertex-model-catalog-refresh.yml`` can automate
# the run (opens a PR when the catalog moves).
#
# Filtering: publisher-scoped subsets are exposed as separate outputs
# (``all_gemini_models`` etc.), computed via ``startswith`` prefix
# matching on the canonical ``publisher/model`` shape. Add new
# publisher subsets in ``outputs.tf`` as future phases start
# supporting them.

locals {
  all_models    = jsondecode(file("${path.module}/all_models.json"))
  gemini_models = [for m in local.all_models : m if startswith(m, "google/gemini-")]
}
