#!/bin/bash
# Local development launcher for app_pro.py
# Starts gcloud proxy tunnels for each backend service, then runs the UI.
# No OIDC tokens needed — the app skips auth for localhost targets.
#
# Usage (from repo root):  bash ui/start_dev.sh
# Stop:                    Ctrl-C  (kills proxies automatically)

set -euo pipefail

PROJECT=pharma-reguatory-author
REGION=us-central1

CTD_PORT=8081
WRITER_PORT=8083
ANALYST_PORT=8084

cleanup() {
  echo ""
  echo "Stopping proxies…"
  kill "${PIDS[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

PIDS=()

echo "Starting local proxies (gcloud run services proxy)…"
gcloud run services proxy ctd-api \
  --project="$PROJECT" --region="$REGION" --port="$CTD_PORT" &
PIDS+=($!)

gcloud run services proxy ich4-writer \
  --project="$PROJECT" --region="$REGION" --port="$WRITER_PORT" &
PIDS+=($!)

gcloud run services proxy clinical-analyst \
  --project="$PROJECT" --region="$REGION" --port="$ANALYST_PORT" &
PIDS+=($!)

# Give proxies a moment to bind
sleep 2

echo "Proxies running:"
echo "  ctd-api        → http://localhost:$CTD_PORT"
echo "  ich4-writer    → http://localhost:$WRITER_PORT"
echo "  clinical-analyst → http://localhost:$ANALYST_PORT"
echo ""

# Point app at local proxies (no auth needed)
export CTD_API_URL="http://localhost:$CTD_PORT"
export ICH4_WRITER_URL="http://localhost:$WRITER_PORT"
export CLINICAL_ANALYST_URL="http://localhost:$ANALYST_PORT"
export GCS_BUCKET="${GCS_BUCKET:-pharma-reguatory-author-life-science}"
export PYTHONUNBUFFERED=1

# OPENAI_API_KEY: pull from Secret Manager if not already set
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "Fetching OPENAI_API_KEY from Secret Manager…"
  export OPENAI_API_KEY
  OPENAI_API_KEY=$(gcloud secrets versions access latest \
    --secret=OPENAI_API_KEY --project="$PROJECT")
fi

echo "Starting app on http://localhost:7860"
exec python3 -u "$(dirname "$0")/app_pro.py"
