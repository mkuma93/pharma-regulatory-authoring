#!/usr/bin/env bash
# ICH4/content_worker/tasks.sh
#
# Task runner for the ICH4 content worker service.
# Each task is a self-contained function; run one at a time:
#
#   bash ICH4/content_worker/tasks.sh <task>
#
# Available tasks
# ───────────────
#   test    — run unit tests locally (requires .venv)
#   deploy  — build + deploy ich4-content-worker + wire Pub/Sub topic + push subscription
#   logs    — tail live Cloud Run logs for the content worker
#   status  — print Cloud Run service details
#   help    — print this message
#
# Prerequisites
# ─────────────
#   gcloud CLI authenticated and set to the correct project
#   Docker daemon running (for local builds)
#   Python .venv at repo root with pytest installed
#
# Override any default with environment variables:
#   PROJECT_ID, REGION, CONTENT_WORKER_NAME, CONTENT_PUBSUB_TOPIC,
#   CONTENT_PIPELINE_URL, WRITER_URL, PROJECT_NUMBER
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "${SCRIPT_DIR}")")"   # two levels up → repo root

# ── Defaults ──────────────────────────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-pharma-reguatory-author}"
REGION="${REGION:-us-central1}"
CONTENT_WORKER_NAME="${CONTENT_WORKER_NAME:-ich4-content-worker}"
CONTENT_PUBSUB_TOPIC="${CONTENT_PUBSUB_TOPIC:-ich4-content-generation}"
CONTENT_PIPELINE_URL="${CONTENT_PIPELINE_URL:-https://ich4-content-pipeline-811317821863.us-central1.run.app}"
WRITER_URL="${WRITER_URL:-https://ich4-writer-811317821863.us-central1.run.app}"
PROJECT_NUMBER="${PROJECT_NUMBER:-$(gcloud projects describe "${PROJECT_ID}" \
  --format='value(projectNumber)' 2>/dev/null || echo 811317821863)}"


# ── Tasks ─────────────────────────────────────────────────────────────────────

task:test() {
  echo "=== [test] Running content_worker unit tests ==="
  VENV="${REPO_ROOT}/.venv"
  if [[ -f "${VENV}/bin/activate" ]]; then
    # shellcheck disable=SC1090
    source "${VENV}/bin/activate"
  fi
  cd "${REPO_ROOT}"
  python -m pytest ICH4/content_worker/tests/ -v --tb=short
}

task:deploy() {
  echo "=== [deploy] Build + deploy ich4-content-worker ==="
  echo "  Project      : ${PROJECT_ID}"
  echo "  Region       : ${REGION}"
  echo "  Worker       : ${CONTENT_WORKER_NAME}"
  echo "  Topic        : ${CONTENT_PUBSUB_TOPIC}"
  echo "  Content pipeline : ${CONTENT_PIPELINE_URL}"
  echo "  Writer       : ${WRITER_URL}"
  gcloud builds submit "${SCRIPT_DIR}" \
    --config="${SCRIPT_DIR}/cloudbuild.yaml" \
    --project="${PROJECT_ID}" \
    --substitutions="\
_PROJECT_ID=${PROJECT_ID},\
_PROJECT_NUMBER=${PROJECT_NUMBER},\
_REGION=${REGION},\
_WORKER_NAME=${CONTENT_WORKER_NAME},\
_PUBSUB_TOPIC=${CONTENT_PUBSUB_TOPIC},\
_CONTENT_PIPELINE_URL=${CONTENT_PIPELINE_URL},\
_WRITER_URL=${WRITER_URL}"
  echo "=== Content worker deployed and Pub/Sub wired ==="
}

task:logs() {
  echo "=== [logs] Tailing Cloud Run logs for ${CONTENT_WORKER_NAME} (Ctrl-C to stop) ==="
  gcloud beta run services logs tail "${CONTENT_WORKER_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}"
}

task:status() {
  echo "=== [status] Cloud Run service details ==="
  gcloud run services describe "${CONTENT_WORKER_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format="yaml(status.url, status.conditions, status.latestReadyRevisionName, spec.template.spec.containers[0].image)"
}

task:help() {
  sed -n '/^# Available tasks/,/^# Prerequisites/p' "$0" \
    | grep -E "^#   [a-z]" \
    | sed 's/^#   /  /'
}


# ── Dispatch ──────────────────────────────────────────────────────────────────

TASK="${1:-help}"
shift || true   # consume the task name; remaining args passed through

if declare -f "task:${TASK}" > /dev/null; then
  "task:${TASK}" "$@"
else
  echo "[ERROR] Unknown task '${TASK}'"
  echo ""
  task:help
  exit 1
fi
