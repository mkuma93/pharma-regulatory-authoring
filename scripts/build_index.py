"""
Offline preprocessing script — run ONCE before any LangGraph workflows.

Steps:
  1. Parse all ICH guideline PDFs in ctd/guidelines/ using LlamaParse (cloud API)
  2. Build a LlamaIndex VectorStoreIndex from the parsed documents
  3. Persist the index to data/index_store/ (local)
  4. If GCS_BUCKET_NAME is set, upload the index to GCS so Cloud Run can use it

Usage:
  python scripts/build_index.py

This script does NOT need to be re-run unless:
  - New ICH guideline PDFs are added to ctd/guidelines/
  - The embedding model is changed (requires re-indexing)
  - data/index_store/ is deleted

After this script completes, run:
  python main.py extract   # LangGraph: query index → build CTD schema
  -- or --
  Deploy the Cloud Run API (index is served from GCS)
"""
import sys
from pathlib import Path

# Ensure project root is on the path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from src.knowledge.parser import parse_ich_guidelines
from src.knowledge.indexer import build_index


def upload_index_to_gcs(local_index_dir: Path) -> None:
    """Upload the built index directory to GCS for Cloud Run to consume."""
    from google.cloud import storage

    bucket_name = settings.gcs_bucket_name
    prefix = settings.gcs_index_prefix.rstrip("/")

    client = storage.Client()
    bucket = client.bucket(bucket_name)

    files = list(local_index_dir.rglob("*"))
    file_count = sum(1 for f in files if f.is_file())
    print(f"[3/3] Uploading {file_count} index file(s) to gs://{bucket_name}/{prefix}/...")

    for file_path in files:
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(local_index_dir)
        blob_name = f"{prefix}/{relative}"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(file_path))

    print(f"      Done — index available at gs://{bucket_name}/{prefix}/")


def main():
    guidelines_dir = settings.guidelines_dir

    print("=== Offline Index Build ===")
    print(f"  Guidelines dir : {guidelines_dir}")
    print(f"  Index store    : {settings.index_persist_dir}")
    if settings.gcs_bucket_name:
        print(f"  GCS destination: gs://{settings.gcs_bucket_name}/{settings.gcs_index_prefix}")
    print()

    # ── Step 1: Parse PDFs with LlamaParse ───────────────────────────────────
    pdf_files = list(guidelines_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"[ERROR] No PDF files found in {guidelines_dir}")
        print("  Download ICH guidelines first and place them in ctd/guidelines/")
        sys.exit(1)

    print(f"[1/3] Parsing {len(pdf_files)} PDF(s) with LlamaParse...")
    for pdf in pdf_files:
        print(f"       {pdf.name}")

    parsed_docs = parse_ich_guidelines(guidelines_dir)
    print(f"      Done — {len(parsed_docs)} document(s) parsed.\n")

    # ── Step 2: Build and persist LlamaIndex locally ──────────────────────────
    print("[2/3] Building vector index and persisting to disk...")
    build_index(parsed_docs)
    print(f"      Done — index saved to {settings.index_persist_dir}\n")

    # ── Step 3: Upload to GCS (only if bucket is configured) ─────────────────
    if settings.gcs_bucket_name:
        upload_index_to_gcs(settings.index_persist_dir)
        print()
        print("Index build + GCS upload complete.")
        print("Next step: deploy Cloud Run API or run `python main.py extract`")
    else:
        print("Index build complete. (GCS_BUCKET_NAME not set — skipping upload)")
        print("Next step: python main.py extract")


if __name__ == "__main__":
    main()
