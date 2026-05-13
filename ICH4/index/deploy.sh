#!/usr/bin/env bash
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
# Values are read from ctd_structure/config/gcp.yaml (single source of truth).
# Override any value with an env var before running:
#   PROJECT_ID=my-project bash deploy.sh
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "${SCRIPT_DIR}")")"   # life-science/
CONFIG_FILE="${REPO_ROOT}/ctd_structure/config/gcp.yaml"

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "[ERROR] Config file not found: ${CONFIG_FILE}"
  exit 1
fi

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
SERVICE_NAME="${SERVICE_NAME:-ich4-index}"
AR_REPO="${AR_REPO:-$(_yaml_get cloudrun.ar_repo)}"
GCS_BUCKET="${GCS_BUCKET:-your-ich-index-bucket-name}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"

echo "=== ICH4 Index — Deploy to Cloud Run ==="
echo "  Project  : ${PROJECT_ID}"
echo "  Region   : ${REGION}"
echo "  Service  : ${SERVICE_NAME}"
echo "  Image    : ${IMAGE}"
echo "  GCS      : gs://${GCS_BUCKET}/index_store"
echo "  AR repo  : ${AR_REPO}"
echo ""

# ── Prerequisites check ───────────────────────────────────────────────────────
if ! command -v gcloud &>/dev/null; then
  echo "[ERROR] gcloud CLI not found. Install it from https://cloud.google.com/sdk"
  exit 1
fi
if ! command -v docker &>/dev/null; then
  echo "[ERROR] docker not found. Install Docker Desktop."
  exit 1
fi

# ── Authenticate Docker with GCR ─────────────────────────────────────────────
echo "[1/5] Configuring Docker auth for GCR..."
gcloud auth configure-docker --quiet

# ── Enable required GCP APIs (idempotent) ────────────────────────────────────
echo "[2/5] Enabling GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  --project="${PROJECT_ID}" \
  --quiet

# ── Create GCS bucket if it doesn't exist ────────────────────────────────────
echo "[3/5] Ensuring GCS bucket exists..."
if ! gsutil ls "gs://${GCS_BUCKET}" &>/dev/null; then
  gsutil mb -p "${PROJECT_ID}" -l "${REGION}" "gs://${GCS_BUCKET}"
  echo "      Created gs://${GCS_BUCKET}"
else
  echo "      gs://${GCS_BUCKET} already exists."
fi

# ── Verify secrets exist in Secret Manager ───────────────────────────────────
echo "[4/5] Verifying secrets in Secret Manager..."
for SECRET in OPENAI_API_KEY LLAMA_CLOUD_API_KEY; do
  if gcloud secrets describe "${SECRET}" --project="${PROJECT_ID}" &>/dev/null; then
    echo "      ✓ ${SECRET} found."
  else
    echo "[ERROR] Secret '${SECRET}' not found in project '${PROJECT_ID}'."
    echo "        Create it with:"
    echo "          echo -n 'YOUR_KEY' | gcloud secrets create ${SECRET} --data-file=- --project=${PROJECT_ID}"
    exit 1
  fi
done

# ── Build and deploy via Cloud Build ─────────────────────────────────────────
echo "[5/5] Submitting build and deploy to Cloud Build..."
gcloud builds submit "${SCRIPT_DIR}" \
  --config="${SCRIPT_DIR}/cloudbuild.yaml" \
  --project="${PROJECT_ID}" \
  --substitutions="_PROJECT_ID=${PROJECT_ID},_REGION=${REGION},_SERVICE_NAME=${SERVICE_NAME},_GCS_BUCKET=${GCS_BUCKET},_AR_REPO=${AR_REPO}"

# ── Print service URL ─────────────────────────────────────────────────────────
echo ""
echo "=== Deploy complete ==="
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')
echo "  Service URL : ${SERVICE_URL}"
echo "  Health check: ${SERVICE_URL}/health"
echo "  Query endpoint    : POST ${SERVICE_URL}/index/query"
echo "  Templates endpoint: POST ${SERVICE_URL}/templates/generate"
echo ""
echo "Example:"
echo "  curl -X POST ${SERVICE_URL}/templates/generate \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"program\":{\"drug_name\":\"prednisolone\",\"disease_type\":\"bells_palsy\",\"therapeutic_area\":\"neurology\"}}' "
