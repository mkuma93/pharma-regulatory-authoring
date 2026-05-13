#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SOURCE_BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"
TARGET_BRANCH="${2:-$SOURCE_BRANCH}"

if [[ -z "$SOURCE_BRANCH" || "$SOURCE_BRANCH" == "HEAD" ]]; then
  echo "[ERROR] Could not determine source branch."
  echo "Usage: scripts/mirror_to_public.sh <source-branch> [target-branch]"
  exit 1
fi

# Force explicit safe-branch intent for public mirroring.
if [[ ! "$SOURCE_BRANCH" =~ ^public/ ]]; then
  echo "[ERROR] Public mirror is allowed only from branches named 'public/*'."
  echo "        Create one first, for example: git switch -c public/main"
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "[ERROR] Working tree is not clean. Commit/stash before mirroring."
  exit 1
fi

echo "[1/2] Running public safety scan..."
bash scripts/check_public_safety.sh

echo "[2/2] Pushing ${SOURCE_BRANCH} -> origin/${TARGET_BRANCH}"
git push origin "${SOURCE_BRANCH}:${TARGET_BRANCH}"

echo "Done: mirrored ${SOURCE_BRANCH} to origin/${TARGET_BRANCH}."
