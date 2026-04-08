"""GCS helpers for loading templates and saving final documents."""
from __future__ import annotations

import logging

from .gcs_client import gcs, program_prefix
from .models import ProgramInfo, SectionDocument

logger = logging.getLogger(__name__)


def list_template_paths(bucket_name: str, program: ProgramInfo) -> list[str]:
    """Return all approved template .md paths under the program's templates/ prefix."""
    prefix = f"{program_prefix(program)}/templates/"
    bkt    = gcs().bucket(bucket_name)
    return [b.name for b in bkt.list_blobs(prefix=prefix) if b.name.endswith(".md")]


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


def save_document(bucket_name: str, program: ProgramInfo, doc: SectionDocument) -> str:
    """Write a completed document to:
      therapeutic-area/{ta}/{dis}/{drug}/ctd/{module_key}/{section_key}/document.md
    Returns the GCS path.
    """
    prefix   = program_prefix(program)
    gcs_path = f"{prefix}/ctd/{doc.module_key}/{doc.section_key}/document.md"
    bkt      = gcs().bucket(bucket_name)
    bkt.blob(gcs_path).upload_from_string(
        doc.content,
        content_type="text/markdown; charset=utf-8",
    )
    logger.info("[storage] Saved document to gs://%s/%s", bucket_name, gcs_path)
    return gcs_path
