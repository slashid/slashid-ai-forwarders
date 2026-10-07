# Provider + shared locals.
#
# The module runs against the customer's own GCP project — no SlashID
# infrastructure sits between the customer and their data.
#
# One service, two routes. ``POST /{path}`` is the Inference hooks
# endpoint; ``POST /tick`` drives the compliance readers and the deadline
# flush. Which of the two does anything is decided by the credentials
# below, not by a mode flag: hook-only, compliance-only and both are
# configurations of one image.

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  # ``nonsensitive`` around the comparison, never around the secret.
  # Whether a credential was supplied decides the shape of the whole
  # deployment, so it has to reach ``count``, ``for_each``, a lifecycle
  # precondition and two outputs — and Terraform refuses a sensitive
  # value in any of them, which is why ``terraform validate`` fails
  # outright without this. The booleans carry no part of the secret:
  # they are the result of an emptiness test, not a projection of it.
  signing_secret_set = nonsensitive(var.hook_signing_secret != "")
  compliance_enabled = nonsensitive(var.compliance_key != "")
  # Public ingress. Without a compliance key the hook is the only capability, signed or not.
  hook_enabled = local.signing_secret_set || !local.compliance_enabled

  # The release workflow tags the image with the bare version from
  # anthropic/pyproject.toml: ``anthropic-v0.1.0`` → ``:0.1.0``.
  version_short = trimprefix(var.release_version, "anthropic-v")
  image = var.image != "" ? var.image : join("", [
    "${var.region}-docker.pkg.dev/${var.project_id}/${var.registry_repository_id}",
    "/slashid/slashid-ai-forwarder-anthropic:${local.version_short}",
  ])

  # The cadence is an input as a NUMBER (the service compares it against
  # the tombstone TTL at startup); the cron string is derived from it.
  # ``tick_interval_seconds`` is validated to divide an hour or a day, so
  # neither branch can produce a fractional step.
  tick_minutes  = var.tick_interval_seconds / 60
  tick_schedule = local.tick_minutes < 60 ? "*/${local.tick_minutes} * * * *" : "0 */${local.tick_minutes / 60} * * *"
}

resource "google_project_service" "required" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    # The ``google_project`` data source below reads the project number
    # through it, and every project-level IAM binding in iam.tf goes
    # through it too. It is usually already on, and "usually" is how a
    # first apply into a fresh project fails.
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  service = each.value
  # Leaving APIs enabled after ``terraform destroy`` is safer in a shared
  # project, and they are free once enabled.
  disable_on_destroy         = false
  disable_dependent_services = false
}

data "google_project" "this" {
  depends_on = [google_project_service.required]
}
