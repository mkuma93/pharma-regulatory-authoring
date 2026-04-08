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
SERVICE_NAME="${SERVICE_NAME:-ich4-template}"
AR_REPO="${AR_REPO:-$(_yaml_get cloudrun.ar_repo)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"

echo "=== ICH4 Template Service — Deploy to Cloud Run ==="
echo "  Project : ${PROJECT_ID}"
echo "  Region  : ${REGION}"
echo "  Service : ${SERVICE_NAME}"
echo "  Image   : ${IMAGE}"
echo ""

command -v gcloud &>/dev/null || { echo "[ERROR] gcloud CLI not found."; exit 1; }

echo "[1/4] Configuring Docker auth..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "[2/4] Enabling GCP APIs..."
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project="${PROJECT_ID}" --quiet

echo "[3/4] Verifying secrets..."
for SECRET in OPENAI_API_KEY; do
  if gcloud secrets describe "${SECRET}" --project="${PROJECT_ID}" &>/dev/null; then
    echo "      ✓ ${SECRET} found."
  else
    echo "[ERROR] Secret '${SECRET}' not found in project ${PROJECT_ID}."
    exit 1
  fi
done

echo "[4/4] Submitting build and deploy to Cloud Build..."
gcloud builds submit "${REPO_ROOT}" \
  --config="${SCRIPT_DIR}/cloudbuild.yaml" \
  --project="${PROJECT_ID}" \
  --substitutions="_PROJECT_ID=${PROJECT_ID},_REGION=${REGION},_SERVICE_NAME=${SERVICE_NAME},_AR_REPO=${AR_REPO}"

echo ""
echo "=== Deploy complete ==="
URL=$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" \
      --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null || echo "(URL unavailable)")
echo "  URL: ${URL}"
