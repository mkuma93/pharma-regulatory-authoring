#!/usr/bin/env bash
# ICH4/content_worker/deploy.sh
# Convenience wrapper — submits the Cloud Build pipeline from the project root.
#
# Usage:
#   bash deploy.sh [--orchestrator-url URL] [--writer-url URL]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "${SCRIPT_DIR}")")"
PROJECT_ID="${PROJECT_ID:-pharma-reguatory-author}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-ich4}"
WORKER_NAME="${WORKER_NAME:-ich4-content-worker}"
PUBSUB_TOPIC="${PUBSUB_TOPIC:-ich4-content-generation}"
CONTENT_PIPELINE_URL="${CONTENT_PIPELINE_URL:-https://ich4-content-pipeline-74ugcbbbya-uc.a.run.app}"
WRITER_URL="${WRITER_URL:-https://ich4-writer-74ugcbbbya-uc.a.run.app}"
INDEX_URL="${INDEX_URL:-https://ich4-index-74ugcbbbya-uc.a.run.app}"
CLINICAL_ANALYST_URL="${CLINICAL_ANALYST_URL:-https://clinical-analyst-74ugcbbbya-uc.a.run.app}"

PROJECT_NUMBER="${PROJECT_NUMBER:-$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null || echo 811317821863)}"

echo "=== Deploying ${WORKER_NAME} ==="
    echo "    Content pipeline     : ${CONTENT_PIPELINE_URL}"
echo "    Writer               : ${WRITER_URL}"
echo "    Index                : ${INDEX_URL}"
echo "    Clinical analyst     : ${CLINICAL_ANALYST_URL}"
echo "    Pub/Sub topic        : ${PUBSUB_TOPIC}"
echo ""

gcloud builds submit "${REPO_ROOT}" \
  --config="${SCRIPT_DIR}/cloudbuild.yaml" \
  --substitutions="\
_PROJECT_ID=${PROJECT_ID},\
_PROJECT_NUMBER=${PROJECT_NUMBER},\
_REGION=${REGION},\
_AR_REPO=${AR_REPO},\
_WORKER_NAME=${WORKER_NAME},\
_PUBSUB_TOPIC=${PUBSUB_TOPIC},\
_CONTENT_PIPELINE_URL=${CONTENT_PIPELINE_URL},\
_WRITER_URL=${WRITER_URL},\
_INDEX_URL=${INDEX_URL},\
_CLINICAL_ANALYST_URL=${CLINICAL_ANALYST_URL}" \
  --project="${PROJECT_ID}"

echo ""
echo "=== Deploy complete ==="
URL=$(gcloud run services describe "${WORKER_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)" 2>/dev/null || echo "(not found)")
echo "    Service URL: ${URL}"
