# SlashID Vertex forwarder — Model Garden discovery

Sub-module that queries **Vertex Model Garden at plan time** and
exposes the current publisher-model catalog as Terraform outputs.

```hcl
module "slashid_models" {
  source     = "github.com/slashid/slashid-ai-forwarders//vertex/deploy/terraform/models?ref=vertex-v0.1.0"
  project_id = "customer-project-123456"
}

module "slashid_vertex_forwarder" {
  source          = "github.com/slashid/slashid-ai-forwarders//vertex/deploy/terraform?ref=vertex-v0.1.0"
  project_id      = "customer-project-123456"
  region          = "us-central1"
  observed_models = module.slashid_models.all_gemini_models
  # ...
}
```

## Outputs

| output | scope |
| --- | --- |
| `all_gemini_models` | Every `google/gemini-*` publisher model currently listed. |
| `all_models` | Every publisher model currently listed (spans every publisher Google has onboarded — google, anthropic, meta, mistralai, ai21, …). Phase 3.1 forwards only Gemini `generateContent`, so enrolling non-Gemini publishers here provisions BigQuery + logging for models the forwarder cannot yet process. |

## Runtime dependency

The `data "external"` block calls out to `bash` + `gcloud` + `jq` at
plan time. `gcloud` must be authenticated for `var.project_id`. All
three are already required by the parent module (the parent module's
`setPublisherModelConfig` provisioner uses `gcloud`), so this
sub-module adds no new tooling requirement.

## Non-determinism — read before adopting

Google adds and deprecates publisher models on its own cadence. Two
`terraform plan` runs weeks apart will legitimately show diffs when
Model Garden's catalog moves — a new Gemini variant appears, TF
proposes enrolling it; a deprecated one disappears, TF proposes
de-enrolling. **Review the `observed_models` diff on every apply.**

Cost implication: every enrolled model provisions a BigQuery table
and turns on Vertex request-response logging. `all_gemini_models` is
the recommended default. Splatting `all_models` provisions the
broader Model Garden — which today includes rawPredict-only
publishers the forwarder does not yet handle.

If deterministic plans matter more than auto-refresh (e.g. CI-heavy
teams), pass an explicit list to `observed_models` instead of using
this sub-module.
