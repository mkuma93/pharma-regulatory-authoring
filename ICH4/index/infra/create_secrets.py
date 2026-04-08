"""
Push API keys to GCP Secret Manager.

Reads config from:
  - cloudbuild.yaml   → GCP project ID
  - config/.env       → API key values

Usage (from ICH4/index/):
  python scripts/create_secrets.py
"""

import subprocess
from pathlib import Path

import yaml
from dotenv import dotenv_values
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound, AlreadyExists

# ── Paths ──────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent   # ICH4/index/
CLOUDBUILD = ROOT / "cloudbuild.yaml"
ENV_FILE = ROOT / "config" / ".env"

# ── Secrets to push (secret name → .env key) ──────────────────────────────

SECRETS = {
    "OPENAI_API_KEY":      "OPENAI_API_KEY",
    "LLAMA_CLOUD_API_KEY": "LLAMA_CLOUD_API_KEY",
}

# ── Load config ────────────────────────────────────────────────────────────

assert CLOUDBUILD.exists(), f"Not found: {CLOUDBUILD}"
assert ENV_FILE.exists(), (
    f"Not found: {ENV_FILE}\n"
    f"Copy config/.env.example → config/.env and fill in your API keys first."
)

with open(CLOUDBUILD) as f:
    cb = yaml.safe_load(f)

project_id: str = cb["substitutions"]["_PROJECT_ID"]
assert project_id != "your-gcp-project-id", (
    "_PROJECT_ID is still a placeholder in cloudbuild.yaml. Set it to your real project ID."
)

env = dotenv_values(ENV_FILE)

print(f"Project : {project_id}")
print(f"Env file: {ENV_FILE}")
print()

# ── Enable Secret Manager API ──────────────────────────────────────────────

print("Enabling Secret Manager API...")
subprocess.run(
    ["gcloud", "services", "enable", "secretmanager.googleapis.com",
     f"--project={project_id}"],
    check=True, capture_output=True,
)

# ── Push each secret ───────────────────────────────────────────────────────

client = secretmanager.SecretManagerServiceClient()
parent = f"projects/{project_id}"

for secret_name, env_key in SECRETS.items():
    value = env.get(env_key, "").strip()

    if not value or "your_" in value:
        print(f"  SKIP  {secret_name} — empty or placeholder in .env")
        continue

    secret_path = f"{parent}/secrets/{secret_name}"
    payload = value.encode("utf-8")

    # Create secret resource if it doesn't exist yet
    try:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_name,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        print(f"  CREATE  {secret_name}")
    except AlreadyExists:
        print(f"  EXISTS  {secret_name} — adding new version")

    # Add the secret value as a new version
    client.add_secret_version(
        request={
            "parent": secret_path,
            "payload": {"data": payload},
        }
    )
    print(f"  PUSHED  {secret_name}")

# ── Grant Cloud Run service account access ─────────────────────────────────

result = subprocess.run(
    ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
    check=True, capture_output=True, text=True,
)
project_number = result.stdout.strip()
sa = f"{project_number}-compute@developer.gserviceaccount.com"

print()
print(f"Granting secretAccessor to Cloud Run SA: {sa}")

for secret_name in SECRETS:
    secret_path = f"{parent}/secrets/{secret_name}"
    try:
        policy = client.get_iam_policy(request={"resource": secret_path})
        member = f"serviceAccount:{sa}"
        role = "roles/secretmanager.secretAccessor"

        # Add binding only if not already present
        already_granted = any(
            b.role == role and member in b.members
            for b in policy.bindings
        )
        if not already_granted:
            policy.bindings.add(role=role, members=[member])
            client.set_iam_policy(request={"resource": secret_path, "policy": policy})
            print(f"  GRANTED {secret_name}")
        else:
            print(f"  ALREADY GRANTED {secret_name}")
    except NotFound:
        pass  # secret was skipped above

# ── Done ───────────────────────────────────────────────────────────────────

print()
print("Done. Add this to your Cloud Run deploy:")
print(f"  GCP_PROJECT_ID={project_id}")
print()
print("Verify with:")
print(f"  gcloud secrets list --project={project_id}")
