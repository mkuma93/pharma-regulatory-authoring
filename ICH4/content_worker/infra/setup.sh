#!/usr/bin/env bash
# ICH4/content_worker/infra/setup.sh
#
# One-time Pub/Sub + IAM setup for the content worker.
# Run this ONCE from your local machine (with owner/editor + iam.admin rights):
#
#   PROJECT_ID=your-gcp-project-id bash ICH4/content_worker/infra/setup.sh
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-your-gcp-project-id}"
REGION="${REGION:-us-central1}"
WORKER_NAME="${WORKER_NAME:-ich4-content-worker}"
PUBSUB_TOPIC="${PUBSUB_TOPIC:-ich4-content-generation}"
SA_NAME="${SA_NAME:-ich4-content-worker}"
# The push-auth SA must be a user-managed SA (not the Pub/Sub service agent).
# We reuse the invoker SA that was created for ctd-extraction — same pattern.
INVOKER_SA="${INVOKER_SA:-ctd-pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com}"

echo "=== Content Worker — one-time Pub/Sub + IAM setup ==="
echo "    Project      : ${PROJECT_ID}"
echo "    Worker       : ${WORKER_NAME}"
echo "    Topic        : ${PUBSUB_TOPIC}"
echo ""

# ── 1. Enable required APIs ───────────────────────────────────────────────────
echo "[1/5] Enabling APIs..."
gcloud services enable \
  pubsub.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  --project="${PROJECT_ID}" --quiet

# ── 2. Get worker URL ─────────────────────────────────────────────────────────
echo "[2/5] Getting worker URL..."
WORKER_URL=$(gcloud run services describe "${WORKER_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format='value(status.url)' 2>/dev/null || echo "")

if [[ -z "${WORKER_URL}" ]]; then
  echo "ERROR: Worker service '${WORKER_NAME}' not found. Run deploy.sh first."
  exit 1
fi
PUSH_URL="${WORKER_URL}/generate"
echo "    Push endpoint: ${PUSH_URL}"

# ── 3. Create Pub/Sub topic ───────────────────────────────────────────────────
echo "[3/5] Creating Pub/Sub topic '${PUBSUB_TOPIC}'..."
gcloud pubsub topics describe "${PUBSUB_TOPIC}" --project="${PROJECT_ID}" &>/dev/null \
  && echo "    Topic already exists — skipping." \
  || gcloud pubsub topics create "${PUBSUB_TOPIC}" --project="${PROJECT_ID}"

# ── 4. Grant invoker SA roles/run.invoker on worker ──────────────────────────
echo "[4/5] Granting ${INVOKER_SA} roles/run.invoker on ${WORKER_NAME}..."
gcloud run services add-iam-policy-binding "${WORKER_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --member="serviceAccount:${INVOKER_SA}" \
  --role="roles/run.invoker" --quiet \
  && echo "    Granted." \
  || echo "    Already bound or failed — check manually."

# ── 5. Create / update push subscription ─────────────────────────────────────
echo "[5/5] Wiring Pub/Sub push subscription '${PUBSUB_TOPIC}-push'..."
SUB_NAME="${PUBSUB_TOPIC}-push"
if gcloud pubsub subscriptions describe "${SUB_NAME}" --project="${PROJECT_ID}" &>/dev/null; then
  gcloud pubsub subscriptions modify-push-config "${SUB_NAME}" \
    --push-endpoint="${PUSH_URL}" \
    --push-auth-service-account="${INVOKER_SA}" \
    --project="${PROJECT_ID}"
  echo "    Updated existing subscription."
else
  gcloud pubsub subscriptions create "${SUB_NAME}" \
    --topic="${PUBSUB_TOPIC}" \
    --push-endpoint="${PUSH_URL}" \
    --push-auth-service-account="${INVOKER_SA}" \
    --ack-deadline=600 \
    --min-retry-delay=30s \
    --max-retry-delay=300s \
    --project="${PROJECT_ID}"
  echo "    Created subscription."
fi

echo ""
echo "=== Setup complete ==="
echo "    Topic        : ${PUBSUB_TOPIC}"
echo "    Subscription : ${SUB_NAME}"
echo "    Push → ${PUSH_URL}"
