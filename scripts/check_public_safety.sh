#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Scan tracked files only.
tracked_files="$(git ls-files)"

if [[ -z "$tracked_files" ]]; then
  echo "No tracked files found."
  exit 0
fi

# Patterns for likely real infrastructure identifiers.
PATTERN="([0-9]{12}[-a-z0-9]*@[^[:space:]\"]*\\.iam\\.gserviceaccount\\.com|service-[0-9]{12}@gcp-sa-iap\\.iam\\.gserviceaccount\\.com|projects/[a-z0-9-]+/locations/[a-z0-9-]+/connectors/[a-zA-Z0-9._-]+|https://[a-z0-9-]+-[a-z0-9]{8,}-[a-z]{2}\\.a\\.run\\.app)"

# Only inspect text-like tracked files.
text_files=$(echo "$tracked_files" | grep -E '\.(md|txt|py|sh|yaml|yml|json|toml|ini|env|cfg)$' || true)

if [[ -z "$text_files" ]]; then
  echo "No tracked text files matched scan filter."
  exit 0
fi

violations=0

echo "Running public safety scan..."

# 1) Hard-sensitive signatures.
if echo "$text_files" | xargs grep -nE "$PATTERN" >/tmp/public_safety_hits.txt 2>/dev/null; then
  echo "\n[FAIL] Sensitive patterns found:"
  cat /tmp/public_safety_hits.txt
  violations=1
fi

# 2) Block obvious hardcoded legacy IDs from this project lineage.
# Exclude this scanner file itself because it intentionally contains the pattern strings.
legacy_hits="$(echo "$text_files" | xargs grep -nE 'pharma-reguatory-author|74ugcbbbya-uc\.a\.run\.app|811317821863' 2>/dev/null | grep -v '^scripts/check_public_safety.sh:' || true)"
if [[ -n "$legacy_hits" ]]; then
  echo "\n[FAIL] Legacy real identifiers found:"
  echo "$legacy_hits" > /tmp/public_safety_legacy_hits.txt
  cat /tmp/public_safety_legacy_hits.txt
  violations=1
fi

rm -f /tmp/public_safety_hits.txt /tmp/public_safety_legacy_hits.txt

if [[ "$violations" -ne 0 ]]; then
  echo "\nPublic safety scan failed. Replace sensitive values with placeholders."
  exit 1
fi

echo "Public safety scan passed."
