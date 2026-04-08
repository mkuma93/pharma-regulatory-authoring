#!/usr/bin/env bash
# ctd_structure/deploy/tasks.sh
#
# Task runner for the CTD Structure service.
# Each task is a self-contained function; run one at a time:
#
#   bash ctd_structure/deploy/tasks.sh <task>
#
# Available tasks
# ───────────────
#   infra          — one-time GCP infrastructure setup (bucket, IAM, APIs)
#   test           — run unit tests locally (requires .venv)
#   build          — build Docker image via Cloud Build only (no deploy)
#   deploy         — full pipeline: test → build → push → Cloud Run deploy
#   deploy-worker          — build + deploy ctd-worker + set up Pub/Sub topic + push subscription
#   deploy-content-worker  — build + deploy ich4-content-worker (template generation + writing pipeline)
#   url            — print the current Cloud Run service URL
#   proxy          — open a local proxy to the Cloud Run service (no public URL needed)
#   logs           — tail live Cloud Run logs
#   status         — print Cloud Run service details (region, URL, last revision)
#   clean-gcs      — delete the cached canonical CTD template from GCS
#                    (forces next app launch to re-run the full LangGraph pipeline)
#   help           — print this message
#
# Prerequisites
# ─────────────
#   gcloud CLI authenticated and set to the correct project
#   Docker daemon running (for local builds)
#   Python .venv at repo root with pytest installed
#
# Config is read from ctd_structure/config/gcp.yaml — override with env vars:
#   PROJECT_ID, REGION, SERVICE_NAME, GCS_BUCKET, ICH_INDEX_URL
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTD_DIR="$(dirname "${SCRIPT_DIR}")"         # ctd_structure/
REPO_ROOT="$(dirname "${CTD_DIR}")"          # repo root
CONFIG_FILE="${CTD_DIR}/config/gcp.yaml"

# ── Load config ───────────────────────────────────────────────────────────────
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
SERVICE_NAME="${SERVICE_NAME:-$(_yaml_get cloudrun.service_name)}"
ICH_INDEX_URL="${ICH_INDEX_URL:-$(_yaml_get cloudrun.ich_index_url)}"
GCS_BUCKET="${GCS_BUCKET:-$(_yaml_get gcs.bucket)}"
AR_REPO="$(_yaml_get cloudrun.ar_repo 2>/dev/null || echo ich4)"

GCS_TEMPLATE_PREFIX="ctd_structure/ctd"


# ── Tasks ─────────────────────────────────────────────────────────────────────

task:infra() {
  echo "=== [infra] One-time GCP infrastructure setup ==="
  bash "${CTD_DIR}/infra/setup.sh" "$@"
}

task:test() {
  echo "=== [test] Running ctd_structure unit tests ==="
  VENV="${REPO_ROOT}/.venv"
  if [[ -f "${VENV}/bin/activate" ]]; then
    # shellcheck disable=SC1090
    source "${VENV}/bin/activate"
  fi
  cd "${REPO_ROOT}"
  python -m pytest tests/test_ctd_structure/ -v --tb=short
}

task:build() {
  echo "=== [build] Cloud Build — build + push image only (no deploy) ==="
  echo "  Project : ${PROJECT_ID}"
  echo "  Region  : ${REGION}"
  echo "  Context : ${REPO_ROOT}"
  gcloud builds submit "${REPO_ROOT}" \
    --config="${SCRIPT_DIR}/cloudbuild.yaml" \
    --project="${PROJECT_ID}" \
    --substitutions=\
"_PROJECT_ID=${PROJECT_ID},\
_REGION=${REGION},\
_SERVICE_NAME=${SERVICE_NAME},\
_ICH_INDEX_URL=${ICH_INDEX_URL},\
_GCS_BUCKET=${GCS_BUCKET}"
}

task:deploy() {
  echo "=== [deploy] Full pipeline: test → build → push → Cloud Run ==="
  bash "${SCRIPT_DIR}/deploy.sh"
  task:url
}

task:deploy-worker() {
  WORKER_NAME="${WORKER_NAME:-$(_yaml_get cloudrun.worker_name 2>/dev/null || echo ctd-worker)}"
  PUBSUB_TOPIC="${PUBSUB_TOPIC:-$(_yaml_get pubsub.topic 2>/dev/null || echo ctd-extraction)}"
  echo "=== [deploy-worker] Build + deploy ctd-worker + Pub/Sub wiring ==="
  echo "  Worker  : ${WORKER_NAME}"
  echo "  Topic   : ${PUBSUB_TOPIC}"
  gcloud builds submit "${REPO_ROOT}" \
    --config="${SCRIPT_DIR}/cloudbuild-worker.yaml" \
    --project="${PROJECT_ID}" \
    --substitutions="\
_PROJECT_ID=${PROJECT_ID},\
_REGION=${REGION},\
_WORKER_NAME=${WORKER_NAME},\
_ICH_INDEX_URL=${ICH_INDEX_URL},\
_GCS_BUCKET=${GCS_BUCKET},\
_PUBSUB_TOPIC=${PUBSUB_TOPIC}"
  echo "=== Worker deployed and Pub/Sub wired ==="
  echo "  Grant Compute SA publish rights:"
  echo "    gcloud pubsub topics add-iam-policy-binding ${PUBSUB_TOPIC} \\"
  echo "      --member=serviceAccount:\$(gcloud projects describe ${PROJECT_ID} --format='value(projectNumber)')-compute@developer.gserviceaccount.com \\"
  echo "      --role=roles/pubsub.publisher --project=${PROJECT_ID}"
}

task:url() {
  echo "=== [url] Cloud Run service URL ==="
  SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format='value(status.url)' 2>/dev/null || echo "(service not found)")
  echo "  ${SERVICE_URL}"
}

task:proxy() {
  PORT="${PORT:-8080}"
  echo "=== [proxy] Local proxy → Cloud Run (http://localhost:${PORT}) ==="
  echo "  Service : ${SERVICE_NAME}  (Ctrl-C to stop)"
  gcloud run services proxy "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --port="${PORT}"
}

task:logs() {
  echo "=== [logs] Tailing Cloud Run logs (Ctrl-C to stop) ==="
  gcloud beta run services logs tail "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}"
}

task:status() {
  echo "=== [status] Cloud Run service details ==="
  gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format="yaml(status.url, status.conditions, status.latestReadyRevisionName, spec.template.spec.containers[0].image)"
}

task:deploy-content-worker() {
  CONTENT_WORKER_DIR="${REPO_ROOT}/ICH4/content_worker"
  ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-https://ich4-orchestrator-811317821863.us-central1.run.app}"
  WRITER_URL="${WRITER_URL:-https://ich4-writer-811317821863.us-central1.run.app}"
  CONTENT_PUBSUB_TOPIC="${CONTENT_PUBSUB_TOPIC:-ich4-content-generation}"
  CONTENT_WORKER_NAME="${CONTENT_WORKER_NAME:-ich4-content-worker}"
  PROJECT_NUMBER="${PROJECT_NUMBER:-$(gcloud projects describe ${PROJECT_ID} --format='value(projectNumber)' 2>/dev/null || echo 811317821863)}"
  echo "=== [deploy-content-worker] Build + deploy ich4-content-worker ==="
  echo "  Orchestrator : ${ORCHESTRATOR_URL}"
  echo "  Writer       : ${WRITER_URL}"
  echo "  Topic        : ${CONTENT_PUBSUB_TOPIC}"
  gcloud builds submit "${CONTENT_WORKER_DIR}" \
    --config="${CONTENT_WORKER_DIR}/cloudbuild.yaml" \
    --project="${PROJECT_ID}" \
    --substitutions="\
_PROJECT_ID=${PROJECT_ID},\
_PROJECT_NUMBER=${PROJECT_NUMBER},\
_REGION=${REGION},\
_WORKER_NAME=${CONTENT_WORKER_NAME},\
_PUBSUB_TOPIC=${CONTENT_PUBSUB_TOPIC},\
_ORCHESTRATOR_URL=${ORCHESTRATOR_URL},\
_WRITER_URL=${WRITER_URL}"
  echo "=== Content worker deployed and Pub/Sub wired ==="
}

task:clean-gcs() {
  echo "=== [clean-gcs] Deleting cached CTD template from GCS ==="
  echo "  Bucket : gs://${GCS_BUCKET}/${GCS_TEMPLATE_PREFIX}/"
  read -r -p "  This forces a full pipeline re-run next time. Continue? [y/N]: " confirm
  if [[ "${confirm,,}" != "y" ]]; then
    echo "  Aborted."
    exit 0
  fi
  gsutil -m rm -r "gs://${GCS_BUCKET}/${GCS_TEMPLATE_PREFIX}/" 2>/dev/null \
    && echo "  Deleted." \
    || echo "  Nothing to delete (prefix did not exist)."
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
