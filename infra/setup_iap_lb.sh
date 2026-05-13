#!/usr/bin/env bash
# =============================================================================
# setup_iap_lb.sh
# Purpose : Set up a Google Cloud HTTPS Load Balancer + Identity-Aware Proxy
#           (IAP) in front of the 'reguatory-ui-pro' Cloud Run service.
# Context : After removing allUsers invoker access, the app returned 403 for
#           all browser traffic.  This script restores access securely via IAP.
# Project : your-gcp-project-id
# Service : reguatory-ui-pro  (us-central1)
# Author  : mritunjay.kmr1@gmail.com
# Date    : 2026-05-03
# =============================================================================
#
# REVERT (emergency restore, takes ~60 seconds):
#   gcloud run services add-iam-policy-binding reguatory-ui-pro \
#     --region=us-central1 --member="allUsers" --role="roles/run.invoker"
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Configuration — edit these before running
# ---------------------------------------------------------------------------
PROJECT="your-gcp-project-id"
REGION="us-central1"
CR_SERVICE="reguatory-ui-pro"

# Name prefix for all resources created by this script
NAME="reguatory-ui-pro"

# Domain you own that will point to this Load Balancer.
# A Google-managed SSL certificate will be issued for it.
# Leave empty to use a self-signed cert (not recommended for production).
DOMAIN="ui-pro.your-domain.com"   # <-- REPLACE with your real domain

# IAP authorized users (iap.httpsResourceAccessor role)
IAP_USERS=(
  "user:mritunjay.kmr1@gmail.com"
  "user:saurabh1bhargava@gmail.com"
  "user:sonimkuma93@gmail.com"
)

# ---------------------------------------------------------------------------
# 1. Confirm project
# ---------------------------------------------------------------------------
echo "=== Step 1: Set active project ==="
gcloud config set project "${PROJECT}"

# ---------------------------------------------------------------------------
# 2. Enable required APIs
# ---------------------------------------------------------------------------
echo "=== Step 2: Enable APIs ==="
gcloud services enable \
  compute.googleapis.com \
  iap.googleapis.com \
  --project="${PROJECT}"

# ---------------------------------------------------------------------------
# 3. Reserve a global static IP address
#    This IP will be your Load Balancer's front-end address.
#    Point your DOMAIN's A record to this IP before cert provisioning.
# ---------------------------------------------------------------------------
echo "=== Step 3: Reserve static global IP ==="
gcloud compute addresses create "${NAME}-ip" \
  --network-tier=PREMIUM \
  --ip-version=IPV4 \
  --global \
  --project="${PROJECT}"

# Print the reserved IP so you can set the DNS A record
echo ">>> Reserved IP:"
gcloud compute addresses describe "${NAME}-ip" --global \
  --format="value(address)" --project="${PROJECT}"
echo "    → Create an A record: ${DOMAIN} -> <above IP>"
echo "    → Wait for DNS propagation before Step 7 (cert provisioning)."

# ---------------------------------------------------------------------------
# 4. Create a Serverless NEG pointing at the Cloud Run service
#    A Serverless NEG lets the Load Balancer route to Cloud Run without
#    needing any VM instances in the backend.
# ---------------------------------------------------------------------------
echo "=== Step 4: Create Serverless NEG ==="
gcloud compute network-endpoint-groups create "${NAME}-neg" \
  --region="${REGION}" \
  --network-endpoint-type=SERVERLESS \
  --cloud-run-service="${CR_SERVICE}" \
  --project="${PROJECT}"

# ---------------------------------------------------------------------------
# 5. Create a global backend service and attach the NEG
#    The backend service is where IAP will be enabled.
# ---------------------------------------------------------------------------
echo "=== Step 5: Create backend service ==="
gcloud compute backend-services create "${NAME}-backend" \
  --load-balancing-scheme=EXTERNAL_MANAGED \
  --global \
  --project="${PROJECT}"

gcloud compute backend-services add-backend "${NAME}-backend" \
  --global \
  --network-endpoint-group="${NAME}-neg" \
  --network-endpoint-group-region="${REGION}" \
  --project="${PROJECT}"

# ---------------------------------------------------------------------------
# 6. Create URL map, target HTTPS proxy, and forwarding rule
# ---------------------------------------------------------------------------
echo "=== Step 6: Create URL map ==="
gcloud compute url-maps create "${NAME}-urlmap" \
  --default-service="${NAME}-backend" \
  --global \
  --project="${PROJECT}"

# ---------------------------------------------------------------------------
# 7. Create a Google-managed SSL certificate for your domain
#    NOTE: Provisioning can take 10-30 minutes.
#          The DNS A record (Step 3) must resolve before the cert activates.
# ---------------------------------------------------------------------------
echo "=== Step 7: Create managed SSL certificate ==="
gcloud compute ssl-certificates create "${NAME}-cert" \
  --domains="${DOMAIN}" \
  --global \
  --project="${PROJECT}"

# ---------------------------------------------------------------------------
# 8. Create the HTTPS target proxy and forwarding rule
# ---------------------------------------------------------------------------
echo "=== Step 8: Create HTTPS proxy + forwarding rule ==="
gcloud compute target-https-proxies create "${NAME}-https-proxy" \
  --url-map="${NAME}-urlmap" \
  --ssl-certificates="${NAME}-cert" \
  --global \
  --project="${PROJECT}"

gcloud compute forwarding-rules create "${NAME}-https-fwd" \
  --address="${NAME}-ip" \
  --target-https-proxy="${NAME}-https-proxy" \
  --ports=443 \
  --load-balancing-scheme=EXTERNAL_MANAGED \
  --global \
  --project="${PROJECT}"

# (Optional) HTTP → HTTPS redirect
echo "=== Step 8b: HTTP -> HTTPS redirect ==="
gcloud compute url-maps import "${NAME}-http-redirect" --global \
  --source /dev/stdin <<'EOF'
kind: compute#urlMap
name: reguatory-ui-pro-http-redirect
defaultUrlRedirect:
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
  httpsRedirect: true
EOF

gcloud compute target-http-proxies create "${NAME}-http-proxy" \
  --url-map="${NAME}-http-redirect" \
  --global \
  --project="${PROJECT}"

gcloud compute forwarding-rules create "${NAME}-http-fwd" \
  --address="${NAME}-ip" \
  --target-http-proxy="${NAME}-http-proxy" \
  --ports=80 \
  --load-balancing-scheme=EXTERNAL_MANAGED \
  --global \
  --project="${PROJECT}"

# ---------------------------------------------------------------------------
# 9. Enable IAP on the backend service
#    You must first configure an OAuth consent screen and create OAuth
#    credentials in the GCP Console:
#      APIs & Services → OAuth consent screen  (set to Internal)
#      APIs & Services → Credentials → Create → OAuth 2.0 Client ID → Web
#    Then fill OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET below.
# ---------------------------------------------------------------------------
echo "=== Step 9: Enable IAP ==="
OAUTH_CLIENT_ID=""       # <-- fill in from GCP Console
OAUTH_CLIENT_SECRET=""   # <-- fill in from GCP Console

if [[ -z "${OAUTH_CLIENT_ID}" || -z "${OAUTH_CLIENT_SECRET}" ]]; then
  echo "ERROR: Set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET before running Step 9."
  echo "       Go to: APIs & Services → Credentials in GCP Console."
  exit 1
fi

gcloud compute backend-services update "${NAME}-backend" \
  --global \
  --iap="enabled,oauth2-client-id=${OAUTH_CLIENT_ID},oauth2-client-secret=${OAUTH_CLIENT_SECRET}" \
  --project="${PROJECT}"

# ---------------------------------------------------------------------------
# 10. Grant IAP access to authorized users
# ---------------------------------------------------------------------------
echo "=== Step 10: Grant IAP access to users ==="
for MEMBER in "${IAP_USERS[@]}"; do
  gcloud iap web add-iam-policy-binding \
    --resource-type=backend-services \
    --service="${NAME}-backend" \
    --member="${MEMBER}" \
    --role="roles/iap.httpsResourceAccessor" \
    --project="${PROJECT}"
  echo "    Granted: ${MEMBER}"
done

# ---------------------------------------------------------------------------
# 11. Lock Cloud Run to only accept traffic from the Load Balancer
#     The LB sends requests using the Google-managed service identity
#     (serviceAccount:service-PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com).
#     We restrict Cloud Run invoker to that account only.
#     First get the project number.
# ---------------------------------------------------------------------------
echo "=== Step 11: Lock Cloud Run to LB service account ==="
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT}" --format="value(projectNumber)")
LB_SA="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"

# Remove any previously granted allUsers (should already be removed)
gcloud run services remove-iam-policy-binding "${CR_SERVICE}" \
  --region="${REGION}" \
  --member="allUsers" \
  --role="roles/run.invoker" \
  --project="${PROJECT}" 2>/dev/null || true

# Allow only the IAP service account
gcloud run services add-iam-policy-binding "${CR_SERVICE}" \
  --region="${REGION}" \
  --member="${LB_SA}" \
  --role="roles/run.invoker" \
  --project="${PROJECT}"

echo "    Cloud Run now accepts traffic only from the Load Balancer."

# ---------------------------------------------------------------------------
# 12. Verify
# ---------------------------------------------------------------------------
echo "=== Step 12: Verify setup ==="
echo "Forwarding rules:"
gcloud compute forwarding-rules list --global --project="${PROJECT}"

echo ""
echo "Backend services:"
gcloud compute backend-services list --global --project="${PROJECT}"

echo ""
echo "SSL certificate status (wait for ACTIVE):"
gcloud compute ssl-certificates describe "${NAME}-cert" \
  --global \
  --format="value(managed.status)" \
  --project="${PROJECT}"

echo ""
echo "=== DONE ==="
echo "Once the SSL cert is ACTIVE, browse to: https://${DOMAIN}"
echo "You should be prompted to sign in with your Google account (IAP)."
echo ""
echo ">>> REVERT COMMAND (emergency restore without LB/IAP) <<<"
echo "gcloud run services add-iam-policy-binding ${CR_SERVICE} \\"
echo "  --region=${REGION} --member=\"allUsers\" --role=\"roles/run.invoker\""
