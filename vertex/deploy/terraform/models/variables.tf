variable "project_id" {
  description = "GCP project — needed so ``gcloud ai model-garden models list`` runs against the customer's project (billing + region affinity)."
  type        = string
}
