#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# project_setup.sh — One-time GCP project setup.
# Run this ONCE before running any builds or deployments.
#
# What it does:
#   1. Enables all required GCP APIs
#   2. Verifies the active account has access to the project
#
# Usage (from ICH4/index/):
#   bash infra/project_setup.sh
# ---------------------------------------------------------------------------

set -euo pipefail

PROJECT_ID=$(grep '_PROJECT_ID:' "$(dirname "$0")/../cloudbuild.yaml" | head -1 | sed 's/.*: *//')
ACTIVE_ACCOUNT=$(gcloud config get-value account 2>/dev/null)

echo "=================================================="
echo " First-Time Project Setup"
echo "=================================================="
echo "  Project : ${PROJECT_ID}"
echo "  Account : ${ACTIVE_ACCOUNT}"
echo ""

# ── Step 1: Enable required APIs ─────────────────────────────────────────────
echo "[1/2] Enabling required GCP APIs (this may take a minute)..."

APIS=(
  cloudbuild.googleapis.com
  run.googleapis.com
  artifactregistry.googleapis.com
  storage.googleapis.com
  secretmanager.googleapis.com
  iap.googleapis.com
  compute.googleapis.com
)

for api in "${APIS[@]}"; do
  echo "      Enabling ${api}..."
  gcloud services enable "${api}" --project="${PROJECT_ID}" --quiet
done

echo "      All APIs enabled."
echo ""

# ── Step 2: Create Artifact Registry repository (idempotent) ─────────────────
REGION=$(grep '_REGION:' "$(dirname "$0")/../cloudbuild.yaml" | head -1 | sed 's/.*: *//')
AR_REPO=$(grep '_AR_REPO:' "$(dirname "$0")/../cloudbuild.yaml" | head -1 | sed 's/.*: *//')

echo "[2/6] Creating Artifact Registry repository: ${AR_REPO} in ${REGION}..."

if gcloud artifacts repositories describe "${AR_REPO}" \
    --location="${REGION}" --project="${PROJECT_ID}" --quiet &>/dev/null; then
  echo "      Already exists — skipping."
else
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --description="ICH4 Docker images" \
    --quiet
  echo "      Created: ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
fi
echo ""

# ── Step 3: Grant Cloud Build service account required roles ──────────────────
echo "[3/6] Granting roles to Cloud Build service account..."

PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

echo "      Cloud Build SA: ${CB_SA}"

CB_ROLES=(
  roles/run.admin
  roles/artifactregistry.writer
  roles/storage.objectAdmin
  roles/iam.serviceAccountUser
  roles/secretmanager.secretAccessor
)

for role in "${CB_ROLES[@]}"; do
  echo "      Binding ${role}..."
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${CB_SA}" \
    --role="${role}" \
    --quiet \
    --condition=None 2>/dev/null | grep -E "^(Updated|bindings)" | head -1 || true
done

echo "      Done."
echo ""

# ── Step 3: Grant Cloud Run compute service account required roles ────────────
echo "[4/6] Granting roles to Cloud Run compute service account..."

CR_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo "      Cloud Run SA: ${CR_SA}"

CR_ROLES=(
  roles/secretmanager.secretAccessor   # read API keys from Secret Manager
  roles/storage.objectViewer           # download index from GCS
  roles/logging.logWriter              # write Cloud Run logs
)

for role in "${CR_ROLES[@]}"; do
  echo "      Binding ${role}..."
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${CR_SA}" \
    --role="${role}" \
    --quiet \
    --condition=None 2>/dev/null | grep -E "^(Updated|bindings)" | head -1 || true
done

echo "      Done."
echo ""

# ── Step 4: Verify user account has project access ────────────────────────────
echo "[5/6] Creating GCS programs bucket (idempotent)..."

GCS_BUCKET=$(grep '_GCS_BUCKET:' "$(dirname "$0")/../cloudbuild.yaml" | head -1 | sed 's/.*: *//')
PROGRAMS_BUCKET="${PROJECT_ID}-programs"

for bucket in "${GCS_BUCKET}" "${PROGRAMS_BUCKET}"; do
  if gcloud storage buckets describe "gs://${bucket}" --project="${PROJECT_ID}" &>/dev/null; then
    echo "      gs://${bucket} already exists — skipping."
  else
    gcloud storage buckets create "gs://${bucket}" \
      --project="${PROJECT_ID}" \
      --location="${REGION}" \
      --uniform-bucket-level-access \
      --public-access-prevention \
      --quiet
    echo "      Created: gs://${bucket}"
  fi
done
echo ""

# ── Step 6: Verify user account has project access ────────────────────────────
echo "[6/6] Verifying IAM access for ${ACTIVE_ACCOUNT}..."

ROLES=$(gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.members:${ACTIVE_ACCOUNT}" \
  --format="value(bindings.role)" 2>/dev/null || true)

if [[ -z "${ROLES}" ]]; then
  echo ""
  echo "  WARNING: No IAM roles found for ${ACTIVE_ACCOUNT} on project ${PROJECT_ID}."
  echo "  Ask the project owner to grant roles/owner or roles/editor."
else
  echo "      Roles assigned to ${ACTIVE_ACCOUNT}:"
  echo "${ROLES}" | sed 's/^/        /'
fi

echo ""
echo "=================================================="
echo " Setup complete. Run these steps in order:"
echo ""
echo "  1. Store API keys in Secret Manager:"
echo "     python infra/create_secrets.py"
echo ""
echo "  2. Build & deploy the ICH index API:"
echo "     python scripts/build_index.py"
echo "     gcloud builds submit --config cloudbuild.yaml --project=${PROJECT_ID}"
echo ""
echo "  3. Scaffold a new drug program in GCS:"
echo "     python scripts/create_program.py --therapeutic-area <area> --drug <drug>"
echo "=================================================="
