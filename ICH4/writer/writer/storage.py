"""GCS helpers for loading templates and saving final documents.

Versioning layout per section:
  .../ctd/{module_key}/{section_key}/
    document.md          ← always the latest
    versions/
      v1.md              ← first-ever write
      v2.md              ← second write, etc.
      manifest.json      ← [{version, timestamp, run_id, author, gcs_path}, ...]
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .gcs_client import gcs, program_prefix
from .models import ProgramInfo, SectionDocument

logger = logging.getLogger(__name__)


def list_template_paths(bucket_name: str, program: ProgramInfo) -> list[str]:
    """Return all approved template .md paths under the program's templates/ prefix."""
    prefix = f"{program_prefix(program)}/templates/"
    bkt    = gcs().bucket(bucket_name)
    return [b.name for b in bkt.list_blobs(prefix=prefix) if b.name.endswith(".md")]


def list_generated_paths(bucket_name: str, program: ProgramInfo) -> list[str]:
    """Return all generated document.md paths under the program's ctd/ prefix.

    Scans therapeutic-area/{ta}/{dis}/{drug}/ctd/{module}/{section}/document.md
    and returns the full GCS blob names for all that exist.
    """
    prefix = f"{program_prefix(program)}/ctd/"
    bkt    = gcs().bucket(bucket_name)
    return [
        b.name for b in bkt.list_blobs(prefix=prefix)
        if b.name.endswith("/document.md")
    ]


def load_template(bucket_name: str, gcs_path: str) -> str:
    """Download and return the text content of a template blob."""
    bkt  = gcs().bucket(bucket_name)
    blob = bkt.blob(gcs_path)
    return blob.download_as_text(encoding="utf-8")


def parse_section_meta_from_path(gcs_path: str) -> tuple[str, str, str]:
    """Extract (module_key, section_key, section_label) from a GCS template path.

    Expected path format:
      .../templates/{module_key}/{section_key}.md
    Returns section_label as a human-readable form of section_key.
    """
    parts      = gcs_path.rstrip("/").split("/")
    filename   = parts[-1]                     # e.g. 2.5_clinical_overview.md
    module_key = parts[-2]                     # e.g. module2
    section_key = filename[:-3]                # strip .md
    section_label = section_key.replace("_", " ").title()
    return module_key, section_key, section_label


# ── Version helpers ───────────────────────────────────────────────────────────

def _versions_prefix(prefix: str, module_key: str, section_key: str) -> str:
    return f"{prefix}/ctd/{module_key}/{section_key}/versions"


def _load_version_manifest(bkt, vprefix: str) -> list[dict]:
    """Return existing version manifest list, or [] if none exists."""
    blob = bkt.blob(f"{vprefix}/manifest.json")
    try:
        if blob.exists():
            return json.loads(blob.download_as_text())
    except Exception as exc:
        logger.warning("[storage] Could not load version manifest: %s", exc)
    return []


def _save_version_manifest(bkt, vprefix: str, manifest: list[dict]) -> None:
    bkt.blob(f"{vprefix}/manifest.json").upload_from_string(
        json.dumps(manifest, indent=2),
        content_type="application/json",
    )


def save_document(
    bucket_name: str,
    program: ProgramInfo,
    doc: SectionDocument,
    run_id: str = "",
    author: str = "",
) -> str:
    """Write a completed document to GCS with version history.

    Before overwriting document.md the existing blob (if any) is copied to
    versions/v{N}.md and recorded in versions/manifest.json.

    Args:
        run_id:  Content-generation run identifier (for traceability).
        author:  User who triggered the write, extracted from IAP header.

    Returns the GCS path of the new document.md.
    """
    prefix   = program_prefix(program)
    gcs_path = f"{prefix}/ctd/{doc.module_key}/{doc.section_key}/document.md"
    vprefix  = _versions_prefix(prefix, doc.module_key, doc.section_key)
    bkt      = gcs().bucket(bucket_name)

    # ── Snapshot current document into versions/ before overwriting ───────────
    current_blob = bkt.blob(gcs_path)
    try:
        if current_blob.exists():
            manifest = _load_version_manifest(bkt, vprefix)
            next_version = len(manifest) + 1
            versioned_path = f"{vprefix}/v{next_version}.md"
            bkt.copy_blob(current_blob, bkt, versioned_path)

            # Save the prompt that produced this version alongside the snapshot
            prompt_path = ""
            if doc.prompt_messages:
                prompt_path = f"{vprefix}/v{next_version}_prompt.json"
                bkt.blob(prompt_path).upload_from_string(
                    json.dumps({
                        "version":   next_version,
                        "run_id":    run_id,
                        "author":    author or "system",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "messages":  doc.prompt_messages,
                    }, indent=2),
                    content_type="application/json",
                )

            manifest.append({
                "version":     next_version,
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "run_id":      run_id,
                "author":      author,
                "gcs_path":    versioned_path,
                "prompt_path": prompt_path,
            })
            _save_version_manifest(bkt, vprefix, manifest)
            logger.info(
                "[storage] Snapshotted v%d → gs://%s/%s (author=%s)",
                next_version, bucket_name, versioned_path, author or "system",
            )
    except Exception as exc:
        # Non-fatal — proceed with write even if versioning fails
        logger.warning("[storage] Versioning failed (non-fatal): %s", exc)

    # ── Write latest ──────────────────────────────────────────────────────────
    bkt.blob(gcs_path).upload_from_string(
        doc.content,
        content_type="text/markdown; charset=utf-8",
    )
    logger.info("[storage] Saved document to gs://%s/%s", bucket_name, gcs_path)
    return gcs_path
