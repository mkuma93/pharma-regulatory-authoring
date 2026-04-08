# ONE-TIME SCRIPT — run once per new drug program to create its GCS scaffold.
"""
create_program.py — Scaffold a new drug program folder structure in GCS.

Creates:
  gs://{bucket}/programs/{therapeutic_area}/{disease_type}/{drug_name}/
      meta.json
      clinical/studies/
      clinical/reports/
      nonclinical/
      templates/
      workflow/status.json

NOTE: ctd/ is NOT created here — it is created by the CTD creation agent
      after human approval of the program structure.

Usage:
  python scripts/create_program.py \\
      --therapeutic-area neurology \\
      --type bells_palsy \\
      --drug prednisolone \\
      --indication "treatment of Bell's Palsy" \\
      --phase NDA

  Additional optional flags:
      --bucket    override GCS bucket (default: from GCS_PROGRAMS_BUCKET env / .env)
      --mechanism "corticosteroid"
      --target    "glucocorticoid receptor"
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import storage


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Scaffold a drug program in GCS.")
    p.add_argument("--therapeutic-area", default=None, help="e.g. neurology, infectious_disease")
    p.add_argument("--type", default=None, dest="disease_type", help="Disease/indication type, e.g. bells_palsy, covid19")
    p.add_argument("--drug", default=None, help="Drug name / INN, e.g. prednisolone")
    p.add_argument("--indication", default=None, help="Therapeutic indication")
    p.add_argument("--phase", default=None, help="Development phase, e.g. Phase1, NDA")
    p.add_argument("--mechanism", default=None, help="Mechanism of action (optional)")
    p.add_argument("--target", default=None, help="Protein target (optional)")
    p.add_argument("--bucket", default=None, help="GCS bucket name (overrides env)")
    args = p.parse_args()

    # Interactive prompts for required fields if not provided via CLI
    if not args.therapeutic_area:
        args.therapeutic_area = input("Therapeutic area (e.g. neurology, infectious_disease): ").strip()
    if not args.disease_type:
        args.disease_type = input("Disease / indication type  (e.g. bells_palsy, covid19):  ").strip()
    if not args.drug:
        args.drug = input("Drug name / INN           (e.g. prednisolone):            ").strip()
    if not args.indication:
        args.indication = input("Indication (press Enter to skip):                         ").strip()
    if not args.phase:
        args.phase = input("Development phase         (e.g. Phase1, NDA):             ").strip()

    if not args.therapeutic_area or not args.disease_type or not args.drug:
        print("ERROR: therapeutic area, type, and drug name are all required.")
        sys.exit(1)

    return args


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_bucket_name(override: str | None) -> str:
    if override:
        return override
    import os
    from dotenv import load_dotenv
    load_dotenv(str(Path(__file__).resolve().parent.parent / "config" / ".env"))
    bucket = os.environ.get("GCS_PROGRAMS_BUCKET", "")
    if not bucket:
        print("ERROR: GCS_PROGRAMS_BUCKET not set. Pass --bucket or add it to config/.env")
        sys.exit(1)
    return bucket


def upload_json(bucket: storage.Bucket, blob_path: str, data: dict) -> None:
    blob = bucket.blob(blob_path)
    blob.upload_from_string(
        json.dumps(data, indent=2),
        content_type="application/json",
    )
    print(f"  created  gs://{bucket.name}/{blob_path}")


def create_placeholder(bucket: storage.Bucket, blob_path: str) -> None:
    """GCS has no real folders — upload an empty placeholder to represent one."""
    blob = bucket.blob(blob_path)
    if blob.exists():
        return
    blob.upload_from_string(b"", content_type="application/octet-stream")
    print(f"  created  gs://{bucket.name}/{blob_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    ta       = args.therapeutic_area.lower().replace(" ", "_")
    dtype    = args.disease_type.lower().replace(" ", "_")
    drug     = args.drug.lower().replace(" ", "_")
    base     = f"programs/{ta}/{dtype}/{drug}"

    bucket_name = get_bucket_name(args.bucket)
    client      = storage.Client()
    bucket      = client.bucket(bucket_name)

    print(f"\nScaffolding program: gs://{bucket_name}/{base}/\n")

    # meta.json
    upload_json(bucket, f"{base}/meta.json", {
        "drug_name":          args.drug,
        "therapeutic_area":   args.therapeutic_area,
        "disease_type":       args.disease_type,
        "indication":         args.indication,
        "protein_target":     args.target,
        "mechanism":          args.mechanism,
        "phase":              args.phase,
        "ctd_created":        False,
        "created":            str(date.today()),
    })

    # Folder placeholders
    placeholders = [
        f"{base}/clinical/studies/.keep",
        f"{base}/clinical/reports/.keep",
        f"{base}/nonclinical/.keep",
        f"{base}/templates/.keep",
    ]
    for p in placeholders:
        create_placeholder(bucket, p)

    # workflow/status.json
    upload_json(bucket, f"{base}/workflow/status.json", {
        "ctd_created":          False,
        "templates_generated":  False,
        "content_generated":    False,
        "last_updated":         str(date.today()),
    })

    print(f"\nDone. CTD structure will be created by the CTD agent after approval.")
    print(f"  Next: python scripts/create_ctd.py --therapeutic-area {ta} --type {dtype} --drug {drug}")


if __name__ == "__main__":
    main()
