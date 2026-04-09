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
SERVICE_NAME="${SERVICE_NAME:-ich4-orchestrator}"
AR_REPO="${AR_REPO:-$(_yaml_get cloudrun.ar_repo)}"

# Service URLs — set these after deploying the index and template services
INDEX_SERVICE_URL="${INDEX_SERVICE_URL:-https://ich4-index-811317821863.us-central1.run.app}"
TEMPLATE_SERVICE_URL="${TEMPLATE_SERVICE_URL:-}"

if [[ -z "${TEMPLATE_SERVICE_URL}" ]]; then
  echo "[ERROR] Set TEMPLATE_SERVICE_URL before deploying."
  echo "  Export it or set in ICH4/orchestrator/config/.env"
  exit 1
fi

echo "=== ICH4 Orchestrator — Deploy to Cloud Run ==="
echo "  Project          : ${PROJECT_ID}"
echo "  Region           : ${REGION}"
echo "  Service          : ${SERVICE_NAME}"
echo "  Index URL        : ${INDEX_SERVICE_URL}"
echo "  Template URL     : ${TEMPLATE_SERVICE_URL}"
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
  --substitutions="_PROJECT_ID=${PROJECT_ID},_REGION=${REGION},_SERVICE_NAME=${SERVICE_NAME},_AR_REPO=${AR_REPO},_INDEX_SERVICE_URL=${INDEX_SERVICE_URL},_TEMPLATE_SERVICE_URL=${TEMPLATE_SERVICE_URL}"

echo ""
echo "=== Deploy complete ==="
URL=$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" \
      --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null || echo "(unavailable)")
echo "  URL: ${URL}"
