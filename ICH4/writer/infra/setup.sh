#!/usr/bin/env bash
# infra/setup.sh — one-time IAM setup for the ich4-writer Cloud Run service.
#
# Run this ONCE before the first deploy. Safe to re-run (gcloud IAM bindings are idempotent).
#
# Required: gcloud CLI authenticated as a project Owner or IAM Admin.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/../../../ctd_structure/config/gcp.yaml"

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

[[ -n "${PROJECT_ID}" ]] || { echo "[ERROR] PROJECT_ID not set."; exit 1; }

SA_EMAIL="${SERVICE_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "=== ICH4 Writer — IAM Setup ==="
echo "  Project    : ${PROJECT_ID}"
echo "  Region     : ${REGION}"
echo "  Service SA : ${SA_EMAIL}"
echo ""

# ── 1. Create the service account ─────────────────────────────────────────────
echo "[1/5] Creating service account (idempotent)..."
gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" &>/dev/null \
  || gcloud iam service-accounts create "${SERVICE_NAME}" \
       --display-name="ICH4 Writer Service" \
       --project="${PROJECT_ID}"

# ── 2. Secret Manager access — read OPENAI_API_KEY ────────────────────────────
echo "[2/5] Granting secretmanager.secretAccessor..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None \
  --quiet

# ── 3. GCS access — read templates, write CTD output documents ───────────────
echo "[3/5] Granting storage.objectAdmin on GCS bucket..."
if [[ -n "${GCS_BUCKET_NAME:-}" ]]; then
  gsutil iam ch "serviceAccount:${SA_EMAIL}:roles/storage.objectAdmin" \
    "gs://${GCS_BUCKET_NAME}"
else
  echo "  [warn] GCS_BUCKET_NAME not set — skipping bucket-level IAM. Set and re-run."
fi

# ── 4. Cloud Run — allow Cloud Build SA to deploy ─────────────────────────────
echo "[4/5] Granting Cloud Build SA permission to deploy Cloud Run..."
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/run.admin" \
  --condition=None \
  --quiet

gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/iam.serviceAccountUser" \
  --project="${PROJECT_ID}" \
  --quiet

# ── 5. Ensure OPENAI_API_KEY secret exists ────────────────────────────────────
echo "[5/5] Checking OPENAI_API_KEY secret..."
gcloud secrets describe OPENAI_API_KEY --project="${PROJECT_ID}" &>/dev/null \
  && echo "  OPENAI_API_KEY secret already exists." \
  || echo "  [WARN] Secret 'OPENAI_API_KEY' not found — create it with:"
    echo "         gcloud secrets create OPENAI_API_KEY --project=${PROJECT_ID}"
    echo "         echo -n 'sk-...' | gcloud secrets versions add OPENAI_API_KEY --data-file=- --project=${PROJECT_ID}"

echo ""
echo "=== IAM setup complete ==="
echo "  Next step: GCS_BUCKET_NAME=<bucket> ./deploy.sh"
