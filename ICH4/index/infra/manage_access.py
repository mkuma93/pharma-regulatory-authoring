"""
Grant or revoke individual IAP access to the ICH4 index MVP.

Use this to control exactly who can reach the demo — since you are
paying the Cloud Run / LB costs, you decide who gets in.

Usage (from ICH4/index/):
  python scripts/manage_access.py grant  evaluator@company.com
  python scripts/manage_access.py revoke evaluator@company.com
  python scripts/manage_access.py list
"""

import subprocess
import sys
from pathlib import Path

import yaml
from dotenv import dotenv_values

# ── Config ────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CLOUDBUILD = ROOT / "cloudbuild.yaml"
INFRA_ENV  = Path(__file__).resolve().parent / "infra.env"

assert CLOUDBUILD.exists(), f"Not found: {CLOUDBUILD}"
assert INFRA_ENV.exists(), (
    f"Not found: {INFRA_ENV}\n"
    f"Copy infra/infra.env.example → infra/infra.env and fill in your values."
)

with open(CLOUDBUILD) as f:
    cb = yaml.safe_load(f)

PROJECT_ID   = cb["substitutions"]["_PROJECT_ID"]
SERVICE_NAME = cb["substitutions"]["_SERVICE_NAME"]
BACKEND_NAME = f"{SERVICE_NAME}-backend"

# ── Helpers ────────────────────────────────────────────────────────────────

def run(args: list[str], capture: bool = False) -> str:
    result = subprocess.run(args, capture_output=capture, text=True)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip() if capture else ""


def iap_args(action: str, email: str) -> list[str]:
    return [
        "gcloud", "iap", "web", f"{action}-iam-policy-binding",
        "--resource-type=backend-services",
        f"--service={BACKEND_NAME}",
        f"--member=user:{email}",
        "--role=roles/iap.httpsResourceAccessor",
        f"--project={PROJECT_ID}",
    ]


def cmd_grant(email: str) -> None:
    run(iap_args("add", email))
    print(f"GRANTED  {email}  →  can now access https://your-domain")


def cmd_revoke(email: str) -> None:
    run(iap_args("remove", email))
    print(f"REVOKED  {email}  →  access removed")


def cmd_list() -> None:
    output = run(
        [
            "gcloud", "iap", "web", "get-iam-policy",
            "--resource-type=backend-services",
            f"--service={BACKEND_NAME}",
            f"--project={PROJECT_ID}",
            "--format=json",
        ],
        capture=True,
    )
    import json
    policy = json.loads(output)
    bindings = policy.get("bindings", [])

    allowed = []
    for b in bindings:
        if b.get("role") == "roles/iap.httpsResourceAccessor":
            for member in b.get("members", []):
                allowed.append(member.replace("user:", ""))

    if allowed:
        print(f"Users with access to {SERVICE_NAME}:")
        for email in sorted(allowed):
            print(f"  {email}")
    else:
        print("No users have been granted access yet.")


# ── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    usage = "Usage: python manage_access.py [grant|revoke|list] [email]"

    if len(sys.argv) < 2:
        print(usage)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "list":
        cmd_list()
    elif command in ("grant", "revoke") and len(sys.argv) == 3:
        email = sys.argv[2]
        if command == "grant":
            cmd_grant(email)
        else:
            cmd_revoke(email)
    else:
        print(usage)
        sys.exit(1)
