#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "${SCRIPT_DIR}")")"
CONFIG_FILE="${REPO_ROOT}/ctd_structure/config/gcp.yaml"

[[ -f "${CONFIG_FILE}" ]] || { echo "[ERROR] Config not found: ${CONFIG_FILE}"; exit 1; }

_yaml_get() {
  python3 -c "
import yaml, sys
d = yaml.safe_load(open('${CONFIG_FILE}'))
for k in '$1'.split('.'): d = d[k]
print(d)
" 2>/dev/null
}

PROJECT_ID="${PROJECT_ID:-$(_yaml_get gcp.project_id)}"
REGION="${REGION:-$(_yaml_get gcp.region)}"
SERVICE_NAME="${SERVICE_NAME:-ich4-writer}"
AR_REPO="${AR_REPO:-$(_yaml_get cloudrun.ar_repo)}"

# GCS bucket that holds templates + clinical data
GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-}"

echo "=== ICH4 Writer — Deploy to Cloud Run ==="
echo "  Project          : ${PROJECT_ID}"
echo "  Region           : ${REGION}"
echo "  Service          : ${SERVICE_NAME}"
echo "  GCS Bucket       : ${GCS_BUCKET_NAME:-<set GCS_BUCKET_NAME to override>}"
echo ""

command -v gcloud &>/dev/null || { echo "[ERROR] gcloud CLI not found."; exit 1; }

echo "[1/3] Configuring Docker auth..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "[2/3] Enabling GCP APIs..."
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  --project="${PROJECT_ID}" --quiet

echo "[3/3] Submitting build and deploy to Cloud Build..."
gcloud builds submit "${REPO_ROOT}" \
  --config="${SCRIPT_DIR}/cloudbuild.yaml" \
  --project="${PROJECT_ID}" \
  --substitutions="_PROJECT_ID=${PROJECT_ID},_REGION=${REGION},_SERVICE_NAME=${SERVICE_NAME},_AR_REPO=${AR_REPO},_GCS_BUCKET_NAME=${GCS_BUCKET_NAME}"

echo ""
echo "=== Deploy complete ==="
URL=$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" \
      --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null || echo "(unavailable)")
echo "  URL: ${URL}"
