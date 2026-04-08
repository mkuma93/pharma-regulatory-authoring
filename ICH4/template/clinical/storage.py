"""GCS helpers for loading/saving clinical data manifests.

Manifest path: therapeutic-area/{ta}/{dis}/{drug}/clinical_data/manifest.json
"""
from __future__ import annotations

import json
import logging

from google.cloud import storage

from .models import ClinicalDataManifest
from template.models import ProgramInfo

logger = logging.getLogger(__name__)

_client: storage.Client | None = None


def _gcs() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def _manifest_path(program: ProgramInfo) -> str:
    ta   = program.therapeutic_area.strip().lower().replace(" ", "_")
    dis  = program.disease_type.strip().lower().replace(" ", "_")
    drug = program.drug_name.strip().lower().replace(" ", "_")
    return f"therapeutic-area/{ta}/{dis}/{drug}/clinical_data/manifest.json"


def load_manifest(bucket_name: str, program: ProgramInfo) -> ClinicalDataManifest | None:
    """Return the clinical manifest for *program* from GCS, or None if absent."""
    try:
        bkt  = _gcs().bucket(bucket_name)
        blob = bkt.blob(_manifest_path(program))
        if not blob.exists():
            logger.info("[clinical.storage] No manifest at %s", _manifest_path(program))
            return None
        return ClinicalDataManifest.model_validate_json(blob.download_as_text())
    except Exception as exc:
        logger.warning("[clinical.storage] Failed to load manifest: %s", exc)
        return None


def save_manifest(bucket_name: str, manifest: ClinicalDataManifest) -> str:
    """Upload *manifest* to GCS. Returns the GCS object path."""
    path = _manifest_path(manifest.program)
    bkt  = _gcs().bucket(bucket_name)
    bkt.blob(path).upload_from_string(
        manifest.model_dump_json(indent=2),
        content_type="application/json",
    )
    logger.info("[clinical.storage] Saved manifest to gs://%s/%s", bucket_name, path)
    return path
