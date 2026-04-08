#!/usr/bin/env bash
# ctd_structure/deploy/deploy.sh
#
# Builds and deploys the CTD Structure Gradio demo to Cloud Run.
# Run from any directory:
#   bash ctd_structure/deploy/deploy.sh
#
# Prerequisites:
#   - Run ctd_structure/infra/setup.sh ONCE before the first deploy
#   - gcloud CLI authenticated and configured
#   - Docker daemon running
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTD_DIR="$(dirname "${SCRIPT_DIR}")"             # ctd_structure/
CONTEXT_DIR="$(dirname "${CTD_DIR}")"            # repo root — Cloud Build context
CONFIG_FILE="${CTD_DIR}/config/gcp.yaml"

# ── Load config/gcp.yaml ──────────────────────────────────────────────────────
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "[ERROR] Config file not found: ${CONFIG_FILE}"
  exit 1
fi

_yaml_get() { python3 -c "import yaml,sys; d=yaml.safe_load(open('${CONFIG_FILE}')); keys='$1'.split('.'); [d:=d[k] for k in keys]; print(d)" 2>/dev/null; }

PROJECT_ID="$(_yaml_get gcp.project_id)"
REGION="$(_yaml_get gcp.region)"
SERVICE_NAME="$(_yaml_get cloudrun.service_name)"
ICH_INDEX_URL="$(_yaml_get cloudrun.ich_index_url)"
GCS_BUCKET="$(_yaml_get gcs.bucket)"
# ─────────────────────────────────────────────────────────────────────────────

echo "=== CTD Structure — Deploy Gradio demo to Cloud Run ==="
echo "  Project      : ${PROJECT_ID}"
echo "  Region       : ${REGION}"
echo "  Service      : ${SERVICE_NAME}"
echo "  Build context: ${CONTEXT_DIR} (repo root — tests + ctd_structure/ both available)"
echo "  ICH Index    : ${ICH_INDEX_URL}"
echo "  GCS Bucket   : gs://${GCS_BUCKET}/therapeutic-area/"
echo ""

# ── Prerequisites ────────────────────────────────────────────────────────────
if ! command -v gcloud &>/dev/null; then
  echo "[ERROR] gcloud CLI not found. Install: https://cloud.google.com/sdk"
  exit 1
fi

# ── Infra preflight check ─────────────────────────────────────────────────────
# Verify the three one-time infra conditions. If any are missing, run setup.sh
# automatically so the deploy never fails on a fresh project.
ICH_INDEX_SERVICE="$(_yaml_get cloudrun.ich_index_service)"
_infra_ready=true

if ! gsutil ls "gs://${GCS_BUCKET}" &>/dev/null; then
  echo "[preflight] GCS bucket gs://${GCS_BUCKET} not found."
  _infra_ready=false
fi

if ! gcloud services list --project="${PROJECT_ID}" --filter="name:run.googleapis.com" --format="value(name)" 2>/dev/null | grep -q "run.googleapis.com"; then
  echo "[preflight] Cloud Run API not enabled."
  _infra_ready=false
fi

PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
if ! gcloud run services get-iam-policy "${ICH_INDEX_SERVICE}" \
    --region="${REGION}" --project="${PROJECT_ID}" 2>/dev/null \
    | grep -q "${SA}"; then
  echo "[preflight] IAM binding for ${SA} on ${ICH_INDEX_SERVICE} not found."
  _infra_ready=false
fi

if [[ "${_infra_ready}" == "false" ]]; then
  echo "[preflight] Infra not fully set up — running ctd_structure/infra/setup.sh first..."
  echo ""
  bash "$(dirname "${SCRIPT_DIR}")/infra/setup.sh"
  echo ""
else
  echo "[preflight] Infra already set up — skipping setup.sh."
fi

# ── Submit build to Cloud Build ────────────────────────────────────────────────
echo "[1/2] Submitting build to Cloud Build..."
echo "      Context : ${CONTEXT_DIR}"
echo "      Config  : ${SCRIPT_DIR}/cloudbuild.yaml"
gcloud builds submit "${CONTEXT_DIR}" \
  --config="${SCRIPT_DIR}/cloudbuild.yaml" \
  --project="${PROJECT_ID}" \
  --substitutions=\
"_PROJECT_ID=${PROJECT_ID},\
_REGION=${REGION},\
_SERVICE_NAME=${SERVICE_NAME},\
_ICH_INDEX_URL=${ICH_INDEX_URL},\
_GCS_BUCKET=${GCS_BUCKET}"

# ── Print result ──────────────────────────────────────────────────────────────
echo ""
echo "[2/2] Fetching service URL..."
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')

echo ""
echo "=== Deploy complete ==="
echo "  Gradio demo : ${SERVICE_URL}"
echo ""
echo "The service requires authentication (no-allow-unauthenticated)."
echo "Open via IAP or use:"
echo "  gcloud run services proxy ${SERVICE_NAME} --region=${REGION} --project=${PROJECT_ID}"
echo "  → then open http://localhost:8080"
