"""
One-time IAP infrastructure setup for Cloud Run.

Creates:
  1. Serverless NEG  — points Cloud Run service into the LB
  2. Backend service — attaches NEG, enables IAP
  3. URL map         — routes all traffic to the backend
  4. Managed SSL cert — Google-managed HTTPS for your domain
  5. HTTPS proxy + forwarding rule — public HTTPS entry point

Reads config from cloudbuild.yaml (_PROJECT_ID, _REGION, _SERVICE_NAME)
and config/.env (IAP_DOMAIN, IAP_OAUTH_CLIENT_ID, IAP_OAUTH_CLIENT_SECRET).

Usage (from ICH4/index/):
  python scripts/setup_iap.py

Re-running is safe — each resource is created only if it doesn't exist.

Prerequisites:
  gcloud auth login
  gcloud auth application-default login
  gcloud config set project pharma-reguatory-author

After running:
  - Note the printed BACKEND_SERVICE_ID and set it as IAP_AUDIENCE in Cloud Run:
      /projects/PROJECT_NUMBER/global/backendServices/BACKEND_SERVICE_ID
  - DNS: point your domain's A record to the printed IP address.
  - Wait ~10 min for Google to provision the SSL certificate.
"""

import subprocess
import json
from pathlib import Path

import yaml
from dotenv import dotenv_values

# ── Paths ──────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CLOUDBUILD = ROOT / "cloudbuild.yaml"
ENV_FILE = ROOT / "config" / ".env"

# ── Load config ────────────────────────────────────────────────────────────

assert CLOUDBUILD.exists(), f"Not found: {CLOUDBUILD}"
assert ENV_FILE.exists(), f"Not found: {ENV_FILE} — copy .env.example and fill in values"

with open(CLOUDBUILD) as f:
    cb = yaml.safe_load(f)

sub = cb["substitutions"]
PROJECT_ID   = sub["_PROJECT_ID"]
REGION       = sub["_REGION"]
SERVICE_NAME = sub["_SERVICE_NAME"]

env = dotenv_values(INFRA_ENV)
DOMAIN            = env.get("IAP_DOMAIN", "").strip()
OAUTH_CLIENT_ID   = env.get("IAP_OAUTH_CLIENT_ID", "").strip()
OAUTH_CLIENT_SECRET = env.get("IAP_OAUTH_CLIENT_SECRET", "").strip()
ALLOWED_EMAILS = [
    e.strip() for e in env.get("IAP_ALLOWED_EMAILS", "").split(",") if e.strip()
]

assert DOMAIN, "IAP_DOMAIN not set in config/.env (e.g. ich4.yourdomain.com)"
assert OAUTH_CLIENT_ID, "IAP_OAUTH_CLIENT_ID not set in config/.env"
assert OAUTH_CLIENT_SECRET, "IAP_OAUTH_CLIENT_SECRET not set in config/.env"

# Derived names (all lowercase + hyphens, Cloud Run safe)
NEG_NAME      = f"{SERVICE_NAME}-neg"
BACKEND_NAME  = f"{SERVICE_NAME}-backend"
URLMAP_NAME   = f"{SERVICE_NAME}-urlmap"
CERT_NAME     = f"{SERVICE_NAME}-cert"
PROXY_NAME    = f"{SERVICE_NAME}-https-proxy"
FWD_RULE_NAME = f"{SERVICE_NAME}-https-rule"

print(f"Project      : {PROJECT_ID}")
print(f"Region       : {REGION}")
print(f"Service      : {SERVICE_NAME}")
print(f"Domain       : {DOMAIN}")
print()

# ── Helpers ────────────────────────────────────────────────────────────────

def run(args: list[str], capture: bool = False) -> str:
    """Run a gcloud command. Returns stdout if capture=True."""
    result = subprocess.run(args, capture_output=capture, text=True)
    if result.returncode != 0 and not capture:
        raise RuntimeError(f"Command failed: {' '.join(args)}\n{result.stderr}")
    return result.stdout.strip() if capture else ""


def exists(describe_args: list[str]) -> bool:
    result = subprocess.run(describe_args, capture_output=True)
    return result.returncode == 0


# ── Enable required APIs ───────────────────────────────────────────────────

print("Enabling APIs...")
for api in [
    "compute.googleapis.com",
    "iap.googleapis.com",
    "certificatemanager.googleapis.com",
]:
    run(["gcloud", "services", "enable", api, f"--project={PROJECT_ID}"])
print("  APIs enabled.")
print()

# ── 1. Serverless NEG ─────────────────────────────────────────────────────

if not exists(["gcloud", "compute", "network-endpoint-groups", "describe", NEG_NAME,
               f"--region={REGION}", f"--project={PROJECT_ID}"]):
    print(f"Creating serverless NEG: {NEG_NAME}")
    run([
        "gcloud", "compute", "network-endpoint-groups", "create", NEG_NAME,
        f"--region={REGION}",
        "--network-endpoint-type=serverless",
        f"--cloud-run-service={SERVICE_NAME}",
        f"--project={PROJECT_ID}",
    ])
else:
    print(f"  NEG already exists: {NEG_NAME}")

# ── 2. Backend service ────────────────────────────────────────────────────

if not exists(["gcloud", "compute", "backend-services", "describe", BACKEND_NAME,
               "--global", f"--project={PROJECT_ID}"]):
    print(f"Creating backend service: {BACKEND_NAME}")
    run([
        "gcloud", "compute", "backend-services", "create", BACKEND_NAME,
        "--global",
        "--load-balancing-scheme=EXTERNAL_MANAGED",
        f"--project={PROJECT_ID}",
    ])
    run([
        "gcloud", "compute", "backend-services", "add-backend", BACKEND_NAME,
        "--global",
        f"--network-endpoint-group={NEG_NAME}",
        f"--network-endpoint-group-region={REGION}",
        f"--project={PROJECT_ID}",
    ])
else:
    print(f"  Backend service already exists: {BACKEND_NAME}")

# ── 3. Enable IAP on the backend ──────────────────────────────────────────

print(f"Enabling IAP on backend: {BACKEND_NAME}")
run([
    "gcloud", "compute", "backend-services", "update", BACKEND_NAME,
    "--global",
    f"--iap=enabled,oauth2-client-id={OAUTH_CLIENT_ID},oauth2-client-secret={OAUTH_CLIENT_SECRET}",
    f"--project={PROJECT_ID}",
])
print("  IAP enabled.")

# ── 4. URL map ────────────────────────────────────────────────────────────

if not exists(["gcloud", "compute", "url-maps", "describe", URLMAP_NAME,
               "--global", f"--project={PROJECT_ID}"]):
    print(f"Creating URL map: {URLMAP_NAME}")
    run([
        "gcloud", "compute", "url-maps", "create", URLMAP_NAME,
        f"--default-service={BACKEND_NAME}",
        "--global",
        f"--project={PROJECT_ID}",
    ])
else:
    print(f"  URL map already exists: {URLMAP_NAME}")

# ── 5. Google-managed SSL certificate ────────────────────────────────────

if not exists(["gcloud", "compute", "ssl-certificates", "describe", CERT_NAME,
               "--global", f"--project={PROJECT_ID}"]):
    print(f"Creating managed SSL cert for: {DOMAIN}")
    run([
        "gcloud", "compute", "ssl-certificates", "create", CERT_NAME,
        f"--domains={DOMAIN}",
        "--global",
        f"--project={PROJECT_ID}",
    ])
else:
    print(f"  SSL cert already exists: {CERT_NAME}")

# ── 6. HTTPS target proxy ─────────────────────────────────────────────────

if not exists(["gcloud", "compute", "target-https-proxies", "describe", PROXY_NAME,
               "--global", f"--project={PROJECT_ID}"]):
    print(f"Creating HTTPS proxy: {PROXY_NAME}")
    run([
        "gcloud", "compute", "target-https-proxies", "create", PROXY_NAME,
        f"--url-map={URLMAP_NAME}",
        f"--ssl-certificates={CERT_NAME}",
        "--global",
        f"--project={PROJECT_ID}",
    ])
else:
    print(f"  HTTPS proxy already exists: {PROXY_NAME}")

# ── 7. Forwarding rule (public IP) ────────────────────────────────────────

if not exists(["gcloud", "compute", "forwarding-rules", "describe", FWD_RULE_NAME,
               "--global", f"--project={PROJECT_ID}"]):
    print(f"Creating forwarding rule: {FWD_RULE_NAME}")
    run([
        "gcloud", "compute", "forwarding-rules", "create", FWD_RULE_NAME,
        "--global",
        f"--target-https-proxy={PROXY_NAME}",
        "--ports=443",
        f"--project={PROJECT_ID}",
    ])
else:
    print(f"  Forwarding rule already exists: {FWD_RULE_NAME}")

# ── Print IAP audience + public IP ────────────────────────────────────────

project_number = run(
    ["gcloud", "projects", "describe", PROJECT_ID, "--format=value(projectNumber)"],
    capture=True,
)

backend_id = run(
    ["gcloud", "compute", "backend-services", "describe", BACKEND_NAME,
     "--global", f"--project={PROJECT_ID}", "--format=value(id)"],
    capture=True,
)

public_ip = run(
    ["gcloud", "compute", "forwarding-rules", "describe", FWD_RULE_NAME,
     "--global", f"--project={PROJECT_ID}", "--format=value(IPAddress)"],
    capture=True,
)

iap_audience = f"/projects/{project_number}/global/backendServices/{backend_id}"

# ── Grant access to allowed emails ────────────────────────────────────────

if ALLOWED_EMAILS:
    print()
    print("Granting IAP access to allowed emails...")
    for email in ALLOWED_EMAILS:
        run([
            "gcloud", "iap", "web", "add-iam-policy-binding",
            "--resource-type=backend-services",
            f"--service={BACKEND_NAME}",
            f"--member=user:{email}",
            "--role=roles/iap.httpsResourceAccessor",
            f"--project={PROJECT_ID}",
        ])
        print(f"  GRANTED: {email}")
else:
    print()
    print("  No IAP_ALLOWED_EMAILS set — run scripts/manage_access.py to grant access.")

print()
print("=" * 60)
print("Setup complete.")
print()
print(f"  Public IP  : {public_ip}")
print(f"  Domain     : https://{DOMAIN}")
print()
print("Next steps:")
print(f"  1. Add DNS A record:  {DOMAIN}  →  {public_ip}")
print(f"  2. Wait ~10 min for SSL cert to provision.")
print()
print("  3. Add IAP_AUDIENCE to config/.env and Cloud Run:")
print(f"     IAP_AUDIENCE={iap_audience}")
print()
print("  4. Redeploy Cloud Run with the new env var:")
print(f"     gcloud run services update {SERVICE_NAME} \\")
print(f"       --region={REGION} \\")
print(f"       --update-env-vars IAP_AUDIENCE={iap_audience}")
print("=" * 60)
