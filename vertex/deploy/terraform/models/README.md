# SlashID Vertex forwarder — Model Garden catalog

Zero-resource sub-module that exposes a maintained snapshot of the
Vertex Model Garden publisher-model catalog. Callers splat filtered
subsets into the parent module's `observed_models`.

```hcl
module "slashid_models" {
  source = "github.com/slashid/slashid-ai-forwarders//vertex/deploy/terraform/models?ref=vertex-v0.1.0"
}

module "slashid_vertex_forwarder" {
  source          = "github.com/slashid/slashid-ai-forwarders//vertex/deploy/terraform?ref=vertex-v0.1.0"
  observed_models = module.slashid_models.all_gemini_models
  # ...
}
```

## Outputs

| output | scope |
| --- | --- |
| `all_gemini_models` | Every `google/gemini-*` entry from the snapshot. |
| `all_models` | The full snapshot (spans every publisher onboarded — google, anthropic, meta, mistralai, ai21, …). Phase 3.1 forwards only Gemini `generateContent`; enrolling non-Gemini publishers today provisions BigQuery + logging for models the forwarder cannot yet process. |

Additional publisher subsets (`all_anthropic_models`,
`all_meta_models`, …) will land alongside the phase 3.3+ rawPredict
support.

## How the catalog stays fresh

`all_models.json` is committed to the repo — Terraform reads it at
plan time via `jsondecode(file(...))`, no external calls or tooling
dependency. Plans are deterministic; catalog updates land through
normal PR review.

The catalog itself is refreshed via `refresh.sh`:

```bash
./refresh.sh --project <GCP_PROJECT>
```

Requirements: bash, `gcloud` (authenticated for the project), `jq`.
The script calls `gcloud ai model-garden models list`, canonicalizes
into `publisher/model` form, dedupes and sorts, and rewrites
`all_models.json` — but refuses to overwrite if gcloud returns an
empty list (a defensive check against auth or subcommand failure
silently zeroing the catalog).

A scheduled GitHub Actions workflow runs `refresh.sh` weekly and
opens a PR whenever the catalog moves (see
`.github/workflows/vertex-model-catalog-refresh.yml`). Maintainers
review the PR, verify the diff, and merge.

## Adding new publisher subsets

When phase 3.3+ adds rawPredict support for a new publisher,
add a locals entry + output pair, matching the `gemini` pattern:

```hcl
# main.tf
anthropic_models = [for m in local.all_models : m if startswith(m, "anthropic/")]

# outputs.tf
output "all_anthropic_models" {
  value = local.anthropic_models
}
```
