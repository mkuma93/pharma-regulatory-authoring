#!/usr/bin/env bash
# ctd_structure/infra/setup.sh
#
# ONE-TIME infrastructure setup for the CTD Structure Cloud Run service.
# Run this ONCE before the first deployment (or when the project is new).
# Safe to re-run — all steps are idempotent.
#
# Config is loaded from ctd_structure/config/gcp.yaml.
# CLI flags override the config file values.
#
# Usage:
#   bash ctd_structure/infra/setup.sh
#   bash ctd_structure/infra/setup.sh --project my-other-project
#
# After running this, use deploy.sh for every build/deploy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$(dirname "${SCRIPT_DIR}")/config/gcp.yaml"

# ── Load config/gcp.yaml ──────────────────────────────────────────────────────
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "[ERROR] Config file not found: ${CONFIG_FILE}"
  exit 1
fi

# Parse YAML with yq if available, otherwise fall back to python3
_yaml_get() { python3 -c "import yaml,sys; d=yaml.safe_load(open('${CONFIG_FILE}')); keys='$1'.split('.'); [d:=d[k] for k in keys]; print(d)" 2>/dev/null; }

PROJECT_ID="$(_yaml_get gcp.project_id)"
REGION="$(_yaml_get gcp.region)"
GCS_BUCKET="$(_yaml_get gcs.bucket)"
ICH_INDEX_SERVICE="$(_yaml_get cloudrun.ich_index_service)"
WORKER_NAME="$(_yaml_get cloudrun.worker_name 2>/dev/null || echo ctd-worker)"
PUBSUB_TOPIC="$(_yaml_get pubsub.topic 2>/dev/null || echo ctd-extraction)"
PUBSUB_INVOKER_SA_NAME="ctd-pubsub-invoker"
# ─────────────────────────────────────────────────────────────────────────────

# Simple --project / --region / --bucket flag overrides
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region)  REGION="$2";     shift 2 ;;
    --bucket)  GCS_BUCKET="$2"; shift 2 ;;
    *) echo "[WARN] Unknown argument: $1"; shift ;;
  esac
done

echo "=== CTD Structure — One-time infra setup ==="
echo "  Project      : ${PROJECT_ID}"
echo "  Region       : ${REGION}"
echo "  GCS Bucket   : gs://${GCS_BUCKET}/therapeutic-area/"
echo "  ICH Index svc: ${ICH_INDEX_SERVICE}"
echo "  Worker svc   : ${WORKER_NAME}"
echo "  Pub/Sub topic: ${PUBSUB_TOPIC}"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────
if ! command -v gcloud &>/dev/null; then
  echo "[ERROR] gcloud CLI not found. Install: https://cloud.google.com/sdk"
  exit 1
fi
if ! command -v gsutil &>/dev/null; then
  echo "[ERROR] gsutil not found. It is bundled with the gcloud SDK."
  exit 1
fi

# ── 1. Enable required GCP APIs ───────────────────────────────────────────────
echo "[1/7] Enabling GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  pubsub.googleapis.com \
  iam.googleapis.com \
  --project="${PROJECT_ID}" \
  --quiet
echo "      APIs enabled."

# ── 2. Create GCS bucket (idempotent) ─────────────────────────────────────────
echo "[2/7] Ensuring GCS bucket exists..."
if ! gsutil ls "gs://${GCS_BUCKET}" &>/dev/null; then
  gsutil mb -p "${PROJECT_ID}" -l "${REGION}" "gs://${GCS_BUCKET}"
  echo "      Created gs://${GCS_BUCKET}"
else
  echo "      gs://${GCS_BUCKET} already exists — skipping."
fi

# ── 3. Determine the default Compute service account ─────────────────────────
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo "      Compute service account: ${SA}"

# ── 4. Grant SA permission to invoke the ICH index Cloud Run service ──────────
echo "[3/7] Granting ${SA} roles/run.invoker on ${ICH_INDEX_SERVICE}..."
gcloud run services add-iam-policy-binding "${ICH_INDEX_SERVICE}" \
  --region="${REGION}" \
  --member="serviceAccount:${SA}" \
  --role="roles/run.invoker" \
  --project="${PROJECT_ID}" \
  --quiet 2>/dev/null \
  && echo "      IAM binding set." \
  || echo "      (binding may already exist — continuing)"

# ── 5. Grant SA write access to the GCS bucket ────────────────────────────────
echo "[4/7] Granting ${SA} roles/storage.objectAdmin on gs://${GCS_BUCKET}..."
gsutil iam ch "serviceAccount:${SA}:roles/storage.objectAdmin" "gs://${GCS_BUCKET}" \
  && echo "      Storage binding set." \
  || echo "      (binding may already exist — continuing)"

# ── 6. Create dedicated Pub/Sub push invoker service account ─────────────────
PUBSUB_INVOKER_SA="${PUBSUB_INVOKER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo "[5/7] Ensuring Pub/Sub push invoker SA exists: ${PUBSUB_INVOKER_SA}..."
if ! gcloud iam service-accounts describe "${PUBSUB_INVOKER_SA}" \
     --project="${PROJECT_ID}" &>/dev/null; then
  gcloud iam service-accounts create "${PUBSUB_INVOKER_SA_NAME}" \
    --display-name="CTD Pub/Sub Push Invoker" \
    --project="${PROJECT_ID}"
  echo "      Created SA: ${PUBSUB_INVOKER_SA}"
else
  echo "      SA already exists — skipping."
fi

# ── 7. Grant invoker SA run.invoker on the worker ─────────────────────────────
echo "[6/7] Granting ${PUBSUB_INVOKER_SA} roles/run.invoker on ${WORKER_NAME}..."
gcloud run services add-iam-policy-binding "${WORKER_NAME}" \
  --region="${REGION}" \
  --member="serviceAccount:${PUBSUB_INVOKER_SA}" \
  --role="roles/run.invoker" \
  --project="${PROJECT_ID}" \
  --quiet 2>/dev/null \
  && echo "      IAM binding set." \
  || echo "      (worker may not be deployed yet — run deploy-worker first, then re-run infra)"

# Grant Pub/Sub service agent roles/iam.serviceAccountTokenCreator on invoker SA
# This allows Pub/Sub to mint OIDC tokens for the invoker SA when pushing messages.
PUBSUB_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
echo "      Granting token creator to Pub/Sub service agent ${PUBSUB_SERVICE_AGENT}..."
gcloud iam service-accounts add-iam-policy-binding "${PUBSUB_INVOKER_SA}" \
  --member="serviceAccount:${PUBSUB_SERVICE_AGENT}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project="${PROJECT_ID}" \
  --quiet 2>/dev/null \
  && echo "      Token creator binding set." \
  || echo "      (binding may already exist — continuing)"

# ── 8. Create/update Pub/Sub topic + push subscription ───────────────────────
echo "[7/7] Wiring Pub/Sub topic '${PUBSUB_TOPIC}' → worker push subscription..."

# Topic (idempotent)
gcloud pubsub topics describe "${PUBSUB_TOPIC}" --project="${PROJECT_ID}" &>/dev/null \
  || gcloud pubsub topics create "${PUBSUB_TOPIC}" --project="${PROJECT_ID}"
echo "      Topic: ${PUBSUB_TOPIC}"

# Get worker URL (skip subscription if worker not deployed yet)
WORKER_URL=$(gcloud run services describe "${WORKER_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format='value(status.url)' 2>/dev/null || true)

if [[ -z "${WORKER_URL}" ]]; then
  echo "      ⚠  Worker '${WORKER_NAME}' not deployed yet — skipping subscription."
  echo "         Re-run this script after 'bash tasks.sh deploy-worker' to create it."
else
  PUSH_ENDPOINT="${WORKER_URL}/extract"
  SUB_NAME="${PUBSUB_TOPIC}-push"
  echo "      Push endpoint: ${PUSH_ENDPOINT}"

  # Cloud Run requires the OIDC audience to be the base service URL (no path)
  WORKER_BASE_URL=$(echo "${WORKER_URL}" | cut -d/ -f1-3)

  if gcloud pubsub subscriptions describe "${SUB_NAME}" \
       --project="${PROJECT_ID}" &>/dev/null; then
    gcloud pubsub subscriptions modify-push-config "${SUB_NAME}" \
      --push-endpoint="${PUSH_ENDPOINT}" \
      --push-auth-service-account="${PUBSUB_INVOKER_SA}" \
      --push-auth-token-audience="${WORKER_BASE_URL}" \
      --project="${PROJECT_ID}"
    echo "      Updated push subscription: ${SUB_NAME}"
  else
    gcloud pubsub subscriptions create "${SUB_NAME}" \
      --topic="${PUBSUB_TOPIC}" \
      --push-endpoint="${PUSH_ENDPOINT}" \
      --push-auth-service-account="${PUBSUB_INVOKER_SA}" \
      --push-auth-token-audience="${WORKER_BASE_URL}" \
      --ack-deadline=600 \
      --min-retry-delay=60s \
      --max-retry-delay=600s \
      --project="${PROJECT_ID}"
    echo "      Created push subscription: ${SUB_NAME}"
  fi
fi

echo ""
echo "=== Infra setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Deploy the worker first:"
echo "     bash ctd_structure/deploy/tasks.sh deploy-worker"
echo "  2. Re-run infra to wire the Pub/Sub subscription (if worker was not deployed above):"
echo "     bash ctd_structure/infra/setup.sh"
echo "  3. Deploy the Gradio app:"
echo "     bash ctd_structure/deploy/tasks.sh deploy"
