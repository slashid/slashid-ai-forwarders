# Dynamic Vertex Model Garden discovery.
#
# Calls ``gcloud ai model-garden models list`` at plan time, parses
# the ``publishers/<publisher>/models/<model>`` names into our
# ``publisher/model`` canonical form, sorts (so member set → same
# order → no spurious diff), and exposes:
#
#   - ``all_models``         — every publisher model Model Garden knows.
#   - ``all_gemini_models``  — filtered to ``google/gemini-*``.
#
# Runtime dependency: ``bash``, ``gcloud`` (authenticated for the
# customer project), and ``jq`` on the machine running ``terraform
# plan``. All three are already required by the parent module's
# ``setPublisherModelConfig`` provisioner.
#
# ---
# Non-determinism note (READ BEFORE USING):
#
# Google adds and deprecates publisher models on its own cadence. Two
# ``terraform plan`` runs weeks apart will legitimately show diffs
# when the Model Garden catalog moves — a new Gemini variant appears,
# TF proposes enrolling it (or a deprecated one disappears, TF
# proposes de-enrolling it). Review the ``observed_models`` diff on
# every apply.
#
# Cost implication: every enrolled model provisions a BigQuery table
# and turns on Vertex request-response logging. Auto-enrolling a
# broader publisher scope (e.g. ``google/*``, ``anthropic/*``) has a
# larger blast radius. The ``google/gemini-*`` filter is the
# recommended default.

data "external" "publisher_models" {
  program = ["bash", "-c", <<-EOT
    set -euo pipefail
    gcloud ai model-garden models list \
      --project="${var.project_id}" \
      --format=json \
      | jq -c '{
          models: (
            [.[] | .name | sub("^publishers/"; "") | sub("/models/"; "/")]
            | unique
            | sort
            | join(",")
          )
        }'
  EOT
  ]
}

locals {
  all_models    = split(",", data.external.publisher_models.result.models)
  gemini_models = [for m in local.all_models : m if startswith(m, "google/gemini-")]
}
