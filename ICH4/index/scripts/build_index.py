"""
Offline script — run ONCE locally before deploying.

  1. Parse ICH PDFs with LlamaParse
  2. Build LlamaIndex and persist locally
  3. Upload index to GCS (if GCS_BUCKET_NAME is set)

Usage:
  cd deploy/phase1
  cp config/.env.example config/.env   # fill in your keys
  python scripts/build_index.py

After this completes, deploy the Cloud Run service.
The API will download the index from GCS on cold start.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from knowledge.parser import parse_ich_guidelines
from knowledge.indexer import build_index


def upload_to_gcs(local_dir: Path) -> None:
    from google.cloud import storage

    bucket_name = settings.gcs_bucket_name
    prefix = settings.gcs_index_prefix.rstrip("/")
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    files = [f for f in local_dir.rglob("*") if f.is_file()]
    print(f"[3/3] Uploading {len(files)} file(s) to gs://{bucket_name}/{prefix}/...")
    for file_path in files:
        relative = file_path.relative_to(local_dir)
        blob = bucket.blob(f"{prefix}/{relative}")
        blob.upload_from_filename(str(file_path))
    print(f"      Done — gs://{bucket_name}/{prefix}/")


def main():
    guidelines_dir = settings.guidelines_dir
    print("=== Phase 1 — Build Index ===")
    print(f"  Guidelines : {guidelines_dir}")
    print(f"  Index dir  : {settings.index_persist_dir}")
    if settings.gcs_bucket_name:
        print(f"  GCS target : gs://{settings.gcs_bucket_name}/{settings.gcs_index_prefix}")
    print()

    # Step 1: parse PDFs
    pdfs = list(guidelines_dir.glob("*.pdf"))
    if not pdfs:
        print(f"[ERROR] No PDFs found in {guidelines_dir}")
        print("  Place ICH guideline PDFs in deploy/phase1/guidelines/")
        sys.exit(1)

    print(f"[1/3] Parsing {len(pdfs)} PDF(s)...")
    for p in pdfs:
        print(f"       {p.name}")
    parsed = parse_ich_guidelines(guidelines_dir)
    print(f"      {len(parsed)} document(s) parsed.\n")

    # Step 2: build index
    print("[2/3] Building vector index...")
    build_index(parsed)
    print(f"      Saved to {settings.index_persist_dir}\n")

    # Step 3: upload
    if settings.gcs_bucket_name:
        upload_to_gcs(settings.index_persist_dir)
        print("\nDone. Deploy the Cloud Run service now.")
    else:
        print("[2/3 only] GCS_BUCKET_NAME not set — index is local only.")
        print("Set GCS_BUCKET_NAME in config/.env to enable Cloud Run deployment.")


if __name__ == "__main__":
    main()
