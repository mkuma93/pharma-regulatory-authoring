"""
clinical/storage.py

GCS helpers for clinical data: CSV upload and manifest persistence.

Layout inside the bucket
────────────────────────
  therapeutic-area/{ta}/{disease}/{drug}/clinical_data/
    manifest.json          ← ClinicalDataManifest
    {filename}.csv         ← uploaded raw CSVs
"""
from __future__ import annotations

import json

from google.cloud import storage

from template.models import ProgramInfo

from .models import ClinicalDataManifest

# Lazy singleton
_client: storage.Client | None = None


def _gcs() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def _clinical_prefix(program: ProgramInfo) -> str:
    """GCS prefix for the program's clinical_data folder (no trailing slash)."""
    ta   = program.therapeutic_area.strip().lower().replace(" ", "_")
    dis  = program.disease_type.strip().lower().replace(" ", "_")
    drug = program.drug_name.strip().lower().replace(" ", "_")
    return f"therapeutic-area/{ta}/{dis}/{drug}/clinical_data"


# ── CSV upload ────────────────────────────────────────────────────────────────

def upload_csv(
    bucket_name: str,
    program: ProgramInfo,
    filename: str,
    csv_bytes: bytes,
) -> str:
    """Upload a CSV to the program's clinical_data GCS folder.

    Args:
        bucket_name: Destination GCS bucket.
        program:     Program the CSV belongs to.
        filename:    Target filename inside the clinical_data folder.
        csv_bytes:   Raw CSV bytes to upload.

    Returns:
        Full bucket-relative GCS path of the uploaded file.
    """
    bkt      = _gcs().bucket(bucket_name)
    gcs_path = f"{_clinical_prefix(program)}/{filename}"
    bkt.blob(gcs_path).upload_from_string(csv_bytes, content_type="text/csv; charset=utf-8")
    return gcs_path


# ── Manifest ──────────────────────────────────────────────────────────────────

def load_manifest(bucket_name: str, program: ProgramInfo) -> ClinicalDataManifest | None:
    """Load the clinical data manifest for a program.

    Returns ``None`` if no manifest exists yet (first registration).
    """
    bkt  = _gcs().bucket(bucket_name)
    blob = bkt.blob(f"{_clinical_prefix(program)}/manifest.json")
    if not blob.exists():
        return None
    return ClinicalDataManifest(**json.loads(blob.download_as_text()))


def save_manifest(bucket_name: str, manifest: ClinicalDataManifest) -> str:
    """Persist the manifest JSON to GCS.

    Returns:
        Bucket-relative GCS path where the manifest was written.
    """
    bkt      = _gcs().bucket(bucket_name)
    gcs_path = f"{_clinical_prefix(manifest.program)}/manifest.json"
    bkt.blob(gcs_path).upload_from_string(
        manifest.model_dump_json(indent=2),
        content_type="application/json",
    )
    return gcs_path
