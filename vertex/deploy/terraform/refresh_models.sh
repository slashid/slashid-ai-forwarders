#!/usr/bin/env bash
#
# Refresh ``all_models.json`` from Vertex Model Garden.
#
# Publisher models are a Google-managed catalog (not project-specific)
# but the ``gcloud ai model-garden models list`` endpoint requires an
# authenticated project — pass the project you want billed for the
# API call via ``--project`` OR set ``GCP_PROJECT`` in the environment.
#
# Requirements: bash, gcloud (authenticated), jq.
#
# Usage:
#   ./refresh_models.sh --project <GCP_PROJECT>
#   GCP_PROJECT=<GCP_PROJECT> ./refresh_models.sh
#
# On success: rewrites ``all_models.json`` alongside this script.
# On empty result (auth failure, deprecated command): exits non-zero
# WITHOUT touching the existing file — so a broken CI run cannot
# clobber the catalog with an empty list.

set -euo pipefail

PROJECT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT="$2"
      shift 2
      ;;
    --project=*)
      PROJECT="${1#*=}"
      shift
      ;;
    -h | --help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

PROJECT="${PROJECT:-${GCP_PROJECT:-}}"
if [[ -z "$PROJECT" ]]; then
  echo "error: --project or GCP_PROJECT env var required" >&2
  exit 2
fi

for cmd in gcloud jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: '$cmd' not on PATH" >&2
    exit 2
  fi
done

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/all_models.json"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

gcloud ai model-garden models list \
  --project="$PROJECT" \
  --format=json \
  | jq '
      [
        .[]
        | .name
        | sub("^publishers/"; "")
        | sub("/models/"; "/")
      ]
      | unique
      | sort
    ' >"$TMP"

# Sanity check — if the transform produced an empty array, something
# went wrong (auth, quota, deprecated subcommand). Refuse to overwrite.
COUNT="$(jq 'length' <"$TMP")"
if [[ "$COUNT" -eq 0 ]]; then
  echo "error: gcloud returned zero models — refusing to overwrite $OUT" >&2
  exit 1
fi

mv "$TMP" "$OUT"
echo "refreshed $OUT — $COUNT models"
