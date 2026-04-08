"""
template/storage.py

GCS helpers for persisting and loading per-program CTD content templates.

Layout inside the bucket
────────────────────────
  therapeutic-area/{ta}/{disease}/{drug}/templates/
    manifest.json                     ← TemplateManifest
    {module_key}/{section_key}.md     ← one SectionTemplate per section
"""
from __future__ import annotations

import json
from datetime import date

from google.cloud import storage

from .models import ProgramInfo, SectionTemplate, TemplateManifest

# Lazy singleton so the module can be imported without credentials
_client: storage.Client | None = None


def _gcs() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


# ── Write ─────────────────────────────────────────────────────────────────────

def save_templates(
    bucket_name: str,
    program: ProgramInfo,
    templates: list[SectionTemplate],
) -> TemplateManifest:
    """Write every template and a manifest JSON to GCS.

    Args:
        bucket_name: Destination GCS bucket.
        program:     Program the templates belong to.
        templates:   List of :class:`SectionTemplate` objects to persist.

    Returns:
        The :class:`TemplateManifest` that was written alongside the templates.
    """
    bkt    = _gcs().bucket(bucket_name)
    prefix = program.gcs_prefix
    paths: list[str] = []

    for tmpl in templates:
        gcs_path = f"{prefix}/{tmpl.gcs_path_suffix}"
        blob     = bkt.blob(gcs_path)
        blob.upload_from_string(tmpl.content, content_type="text/markdown; charset=utf-8")
        paths.append(gcs_path)

    manifest = TemplateManifest(
        program=program,
        generated_date=date.today().isoformat(),
        total_templates=len(templates),
        template_paths=paths,
    )
    manifest_blob = bkt.blob(f"{prefix}/manifest.json")
    manifest_blob.upload_from_string(
        manifest.model_dump_json(indent=2),
        content_type="application/json",
    )

    return manifest


# ── Read ──────────────────────────────────────────────────────────────────────

def list_template_paths(bucket_name: str, program: ProgramInfo) -> list[str]:
    """Return all template GCS paths for a program (excludes manifest.json)."""
    bkt    = _gcs().bucket(bucket_name)
    prefix = program.gcs_prefix + "/"
    return [
        b.name
        for b in bkt.list_blobs(prefix=prefix)
        if b.name.endswith(".md")
    ]


def load_template(bucket_name: str, gcs_path: str) -> str:
    """Download and return the raw markdown content of one template."""
    bkt = _gcs().bucket(bucket_name)
    return bkt.blob(gcs_path).download_as_text()


def load_manifest(bucket_name: str, program: ProgramInfo) -> TemplateManifest | None:
    """Load the manifest for a program, or ``None`` if templates haven't been generated."""
    bkt  = _gcs().bucket(bucket_name)
    blob = bkt.blob(f"{program.gcs_prefix}/manifest.json")
    if not blob.exists():
        return None
    return TemplateManifest(**json.loads(blob.download_as_text()))
