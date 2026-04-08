"""
Tear down all Load Balancer resources after the demo.

Deletes (in dependency order):
  1. Forwarding rule       — stops the $0.025/hr charge immediately
  2. HTTPS target proxy
  3. SSL certificate
  4. URL map
  5. Backend service
  6. Serverless NEG

Keeps (costs $0 at rest):
  - Cloud Run service      — scale-to-zero, no charge without requests
  - GCS bucket             — negligible storage cost
  - Artifact Registry      — negligible storage cost
  - Secret Manager secrets — free tier

Reads project/service config from cloudbuild.yaml — no manual input needed.

Usage (from ICH4/index/):
  python scripts/teardown.py
"""

import subprocess
import sys
from pathlib import Path

import yaml

# ── Config ────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CLOUDBUILD = ROOT / "cloudbuild.yaml"

assert CLOUDBUILD.exists(), f"Not found: {CLOUDBUILD}"

with open(CLOUDBUILD) as f:
    cb = yaml.safe_load(f)

sub = cb["substitutions"]
PROJECT_ID   = sub["_PROJECT_ID"]
REGION       = sub["_REGION"]
SERVICE_NAME = sub["_SERVICE_NAME"]

NEG_NAME      = f"{SERVICE_NAME}-neg"
BACKEND_NAME  = f"{SERVICE_NAME}-backend"
URLMAP_NAME   = f"{SERVICE_NAME}-urlmap"
CERT_NAME     = f"{SERVICE_NAME}-cert"
PROXY_NAME    = f"{SERVICE_NAME}-https-proxy"
FWD_RULE_NAME = f"{SERVICE_NAME}-https-rule"

# ── Helpers ────────────────────────────────────────────────────────────────

def delete(args: list[str], label: str) -> None:
    """Delete a resource. Skip gracefully if it doesn't exist."""
    check = subprocess.run(
        # derive describe command from delete command to check existence
        [args[0]] + [args[1]] + ["describe"] + args[3:],
        capture_output=True,
    )
    if check.returncode != 0:
        print(f"  SKIP  {label} (not found)")
        return

    result = subprocess.run(args + ["--quiet"], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  DELETED  {label}")
    else:
        print(f"  FAILED   {label}: {result.stderr.strip()}")


# ── Confirm ────────────────────────────────────────────────────────────────

print(f"Project  : {PROJECT_ID}")
print(f"Service  : {SERVICE_NAME}")
print()
print("This will delete the Load Balancer resources (stops billing).")
print("Cloud Run, GCS, and secrets are kept.")
print()
answer = input("Type 'yes' to confirm: ").strip().lower()
if answer != "yes":
    print("Aborted.")
    sys.exit(0)

print()

# ── Delete in dependency order ─────────────────────────────────────────────

# 1. Forwarding rule — must go first (references the proxy)
delete(
    ["gcloud", "compute", "forwarding-rules", "delete", FWD_RULE_NAME,
     "--global", f"--project={PROJECT_ID}"],
    FWD_RULE_NAME,
)

# 2. HTTPS proxy (references url-map + cert)
delete(
    ["gcloud", "compute", "target-https-proxies", "delete", PROXY_NAME,
     "--global", f"--project={PROJECT_ID}"],
    PROXY_NAME,
)

# 3. SSL certificate
delete(
    ["gcloud", "compute", "ssl-certificates", "delete", CERT_NAME,
     "--global", f"--project={PROJECT_ID}"],
    CERT_NAME,
)

# 4. URL map (references backend)
delete(
    ["gcloud", "compute", "url-maps", "delete", URLMAP_NAME,
     "--global", f"--project={PROJECT_ID}"],
    URLMAP_NAME,
)

# 5. Backend service (references NEG)
delete(
    ["gcloud", "compute", "backend-services", "delete", BACKEND_NAME,
     "--global", f"--project={PROJECT_ID}"],
    BACKEND_NAME,
)

# 6. Serverless NEG
delete(
    ["gcloud", "compute", "network-endpoint-groups", "delete", NEG_NAME,
     f"--region={REGION}", f"--project={PROJECT_ID}"],
    NEG_NAME,
)

# ── Done ───────────────────────────────────────────────────────────────────

print()
print("=" * 55)
print("Teardown complete. Load Balancer billing stopped.")
print()
print("Still running (cost = $0 at rest):")
print(f"  Cloud Run : {SERVICE_NAME}")
print(f"  GCS bucket: {sub.get('_GCS_BUCKET', 'see cloudbuild.yaml')}")
print()
print("To redeploy the LB for the next demo:")
print("  python scripts/setup_iap.py")
print("=" * 55)
