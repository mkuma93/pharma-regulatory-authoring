"""
Create the GCS bucket for storing the pre-built LlamaIndex.

Reads the bucket name and project ID from cloudbuild.yaml.
Safe to re-run — skips creation if the bucket already exists.

Usage (from ICH4/index/):
  python scripts/create_bucket.py
"""

import subprocess
from pathlib import Path

import yaml

# ── Config ────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CLOUDBUILD = ROOT / "cloudbuild.yaml"

assert CLOUDBUILD.exists(), f"Not found: {CLOUDBUILD}"

with open(CLOUDBUILD) as f:
    cb = yaml.safe_load(f)

sub = cb["substitutions"]
PROJECT_ID  = sub["_PROJECT_ID"]
REGION      = sub["_REGION"]
BUCKET_NAME = sub["_GCS_BUCKET"]

print(f"Project : {PROJECT_ID}")
print(f"Bucket  : {BUCKET_NAME}")
print(f"Region  : {REGION}")
print()

# ── Check if bucket exists ────────────────────────────────────────────────

check = subprocess.run(
    ["gcloud", "storage", "buckets", "describe", f"gs://{BUCKET_NAME}",
     f"--project={PROJECT_ID}"],
    capture_output=True,
)

if check.returncode == 0:
    print(f"Bucket already exists: gs://{BUCKET_NAME}")
else:
    # ── Create bucket ──────────────────────────────────────────────────────
    result = subprocess.run(
        [
            "gcloud", "storage", "buckets", "create", f"gs://{BUCKET_NAME}",
            f"--project={PROJECT_ID}",
            f"--location={REGION}",
            "--uniform-bucket-level-access",   # recommended — no per-object ACLs
            "--public-access-prevention",       # no accidental public exposure
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Created: gs://{BUCKET_NAME}")
    else:
        raise RuntimeError(f"Failed to create bucket:\n{result.stderr}")

# ── Update config/.env with GCS_BUCKET_NAME ───────────────────────────────

env_file = ROOT / "config" / ".env"
if env_file.exists():
    content = env_file.read_text()
    if f"GCS_BUCKET_NAME={BUCKET_NAME}" not in content:
        updated = content.replace(
            "GCS_BUCKET_NAME=",
            f"GCS_BUCKET_NAME={BUCKET_NAME}",
        )
        if updated != content:
            env_file.write_text(updated)
            print(f"Updated config/.env → GCS_BUCKET_NAME={BUCKET_NAME}")

print()
print("Next step — build the index and upload it:")
print("  python scripts/build_index.py")
