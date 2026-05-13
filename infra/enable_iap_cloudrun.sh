#!/usr/bin/env bash
# =============================================================================
# enable_iap_cloudrun.sh
# Purpose : Enable Cloud IAP on the 'reguatory-ui-pro' Cloud Run service and
#           grant access to authorised users.
#
# NOTE : Step 1 (toggle IAP ON) MUST be done manually in the GCP Console.
#        All other steps run via gcloud CLI.
#
# Project : your-gcp-project-id
# Service : reguatory-ui-pro  (us-central1)
# =============================================================================

set -euo pipefail

PROJECT="your-gcp-project-id"
REGION="us-central1"
SERVICE="reguatory-ui-pro"
PROJECT_NUMBER="PROJECT_NUMBER"

# ---------------------------------------------------------------------------
# STEP 1 — MANUAL (Console only, ~30 seconds)
# ---------------------------------------------------------------------------
# gcloud does NOT support enabling IAP on Cloud Run via CLI.
# You must do this once in the browser:
#
#   1. Open: https://console.cloud.google.com/security/iap?project=your-gcp-project-id
#   2. Find the "Cloud Run" section → row for "reguatory-ui-pro"
#   3. Click the toggle to turn IAP ON
#   4. If prompted for OAuth consent screen:
#        - Application type : Internal
#        - App name         : Regulatory Author  (any name is fine)
#        - Click Save
#   5. Click "Enable" to confirm
#
# After this, the app URL will redirect to Google Sign-in.
# ---------------------------------------------------------------------------
echo "=== Step 1: MANUAL — see comments above. Toggle IAP ON in the Console. ==="
echo "    URL: https://console.cloud.google.com/security/iap?project=${PROJECT}"
echo "    Press ENTER once you have enabled IAP in the Console, then this"
echo "    script will continue with the CLI steps..."
read -r

# ---------------------------------------------------------------------------
# STEP 2 — Grant IAP access to authorised users
#           (roles/iap.httpsResourceAccessor)
#
# STATUS: Already executed on 2026-05-03. Verified output:
#   bindings:
#   - members:
#     - user:mritunjay.kmr1@gmail.com
#     - user:saurabh1bhargava@gmail.com
#     - user:sonimkuma93@gmail.com
#     role: roles/iap.httpsResourceAccessor
#   etag: BwZQ2hSnnpI=
#
# Re-running is safe (idempotent) — skip if already done.
# ---------------------------------------------------------------------------
echo "=== Step 2: Grant IAP access to authorised users ==="

for USER in \
  "mritunjay.kmr1@gmail.com" \
  "saurabh1bhargava@gmail.com" \
  "sonimkuma93@gmail.com"
do
  echo "  Granting: ${USER}"
  gcloud iap web add-iam-policy-binding \
    --resource-type=cloud-run \
    --service="${SERVICE}" \
    --region="${REGION}" \
    --member="user:${USER}" \
    --role="roles/iap.httpsResourceAccessor" \
    --project="${PROJECT}"
done

# ---------------------------------------------------------------------------
# STEP 3 — Allow the IAP service account to invoke Cloud Run
#           Without this, IAP authenticates the user but Cloud Run itself
#           rejects the forwarded request with 403.
#
# STATUS: Already executed on 2026-05-03. Verified output:
#   bindings:
#   - members:
#     - serviceAccount:service-PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com
#     - user:mritunjay.kmr1@gmail.com
#     - user:saurabh1bhargava@gmail.com
#     - user:sonimkuma93@gmail.com
#     role: roles/run.invoker
#   etag: BwZQ2hoH_T4=
#
# Re-running is safe (idempotent) — skip if already done.
# ---------------------------------------------------------------------------
echo "=== Step 3: Allow IAP service account to invoke Cloud Run ==="
IAP_SA="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding "${SERVICE}" \
  --region="${REGION}" \
  --member="${IAP_SA}" \
  --role="roles/run.invoker" \
  --project="${PROJECT}"

# ---------------------------------------------------------------------------
# STEP 4 — Verify
# ---------------------------------------------------------------------------
echo "=== Step 4: Verify ==="

echo ""
echo "IAP IAM policy (should list 3 users):"
gcloud iap web get-iam-policy \
  --resource-type=cloud-run \
  --service="${SERVICE}" \
  --region="${REGION}" \
  --project="${PROJECT}"

echo ""
echo "Cloud Run invoker policy (should include IAP SA + 3 users):"
gcloud run services get-iam-policy "${SERVICE}" \
  --region="${REGION}" \
  --project="${PROJECT}"

echo ""
echo "=== DONE ==="
echo "Try opening the app: https://${SERVICE}-your-service-id-uc.a.run.app"
echo "You should be redirected to Google Sign-in."
