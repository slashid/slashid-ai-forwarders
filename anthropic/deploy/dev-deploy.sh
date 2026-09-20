#!/usr/bin/env bash
# Development deploy of the receiver to Cloud Run, straight from source via
# Cloud Build. The Terraform module is the customer path; this is for
# iterating against a test tenant with frame capture on.
#
#   anthropic/deploy/dev-deploy.sh PROJECT [REGION]
#
# Secrets are read from Secret Manager at instance start:
#   slashid_anthropic_signing_secret   whsec_... from claude.ai (comma-join two to rotate)
#   slashid_anthropic_push_token       the anthropic connection's push token
# Both are created with a placeholder on first run; add the real values with
#   printf '%s' 'whsec_...' | gcloud secrets versions add slashid_anthropic_signing_secret --data-file=-
# then re-run this script so a new revision picks up ``latest``.
set -euo pipefail

PROJECT="${1:?project id}"
REGION="${2:-us-central1}"
SERVICE=slashid-anthropic-forwarder
SA_ID=slashid-anthropic-sa
SA="${SA_ID}@${PROJECT}.iam.gserviceaccount.com"
REPO=slashid-anthropic
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/forwarder:$(git rev-parse --short HEAD)"
CAPTURE_BUCKET="slashid-anthropic-capture-${PROJECT}"
ENFORCE="${SLASHID_ENFORCE:-false}"
DENY_MARKER="${SLASHID_CAPTURE_DENY_MARKER:-SLASHID_DENY_ME}"

cd "$(git rev-parse --show-toplevel)"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com \
  --project "$PROJECT" --quiet

gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
    --project "$PROJECT" --quiet

for s in slashid_anthropic_signing_secret slashid_anthropic_push_token; do
  gcloud secrets describe "$s" --project "$PROJECT" >/dev/null 2>&1 || {
    gcloud secrets create "$s" --replication-policy automatic --project "$PROJECT" --quiet
    printf '%s' "placeholder" | gcloud secrets versions add "$s" --data-file=- --project "$PROJECT"
  }
done

gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_ID" --display-name "SlashID Anthropic forwarder" \
    --project "$PROJECT" --quiet

gcloud storage buckets describe "gs://${CAPTURE_BUCKET}" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud storage buckets create "gs://${CAPTURE_BUCKET}" --location "$REGION" \
    --uniform-bucket-level-access --project "$PROJECT"
gcloud storage buckets add-iam-policy-binding "gs://${CAPTURE_BUCKET}" \
  --member "serviceAccount:${SA}" --role roles/storage.objectCreator --project "$PROJECT" --quiet >/dev/null
for s in slashid_anthropic_signing_secret slashid_anthropic_push_token; do
  gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:${SA}" \
    --role roles/secretmanager.secretAccessor --project "$PROJECT" --quiet >/dev/null
done

gcloud builds submit --config anthropic/deploy/cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE}" --project "$PROJECT" --quiet .

gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" --project "$PROJECT" \
  --service-account "$SA" --allow-unauthenticated --ingress all \
  --min-instances 1 --max-instances 3 --concurrency 20 --cpu 1 --memory 512Mi \
  --no-cpu-throttling --timeout 30 \
  --set-env-vars "SLASHID_ENDPOINT=https://api.slashid.com,SLASHID_CAPTURE_BUCKET=${CAPTURE_BUCKET},SLASHID_CAPTURE_DENY_MARKER=${DENY_MARKER},SLASHID_ENFORCE=${ENFORCE},SLASHID_PREFLIGHT_ENABLED=false,LOG_LEVEL=INFO" \
  --set-secrets "SLASHID_HOOK_SIGNING_SECRET=slashid_anthropic_signing_secret:latest,SLASHID_PUSH_TOKEN=slashid_anthropic_push_token:latest" \
  --quiet

gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format 'value(status.url)'
