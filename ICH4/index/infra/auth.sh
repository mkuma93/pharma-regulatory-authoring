#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# auth.sh — Authenticate gcloud and set the active project.
# Run this once before using any infra/ or scripts/ commands.
#
# Usage:
#   ./infra/auth.sh
# ---------------------------------------------------------------------------

set -euo pipefail

PROJECT_ID=$(grep '_PROJECT_ID:' "$(dirname "$0")/../cloudbuild.yaml" | head -1 | sed 's/.*: *//')

echo "[1/3] Logging in to gcloud (opens browser)..."
gcloud auth login

echo ""
echo "[2/3] Setting up Application Default Credentials (for Python SDK)..."
gcloud auth application-default login

echo ""
echo "[3/3] Setting active project: ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}"

echo ""
echo "Auth complete. You can now run:"
echo "  python infra/create_bucket.py"
echo "  python infra/create_secrets.py"
echo "  python infra/setup_iap.py"
