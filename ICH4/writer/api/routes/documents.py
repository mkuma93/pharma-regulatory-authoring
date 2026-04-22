"""GET /documents — list and read generated template documents from GCS.
GET /clinical-data/manifest — return clinical column mapping for a program.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config.settings import settings
from writer.gcs_client import gcs
from writer.models import ProgramInfo
from writer.storage import list_template_paths, load_template

logger = logging.getLogger(__name__)
router = APIRouter()


class DocumentListItem(BaseModel):
    gcs_path: str
    module: str
    section_key: str
    section_label: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentListItem]


class DocumentReadResponse(BaseModel):
    gcs_path: str
    content: str


@router.get("/documents", response_model=DocumentListResponse)
def list_documents(
    therapeutic_area: str = Query(...),
    disease_type: str = Query(...),
    drug_name: str = Query(...),
    bucket_name: str = Query(default=None),
) -> DocumentListResponse:
    """List all generated template sections for a program."""
    bucket = bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )

    try:
        paths = list_template_paths(bucket, program)
    except Exception as exc:
        logger.error("Failed to list documents: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    items: list[DocumentListItem] = []
    for path in sorted(paths):
        parts = path.rstrip("/").split("/")
        filename = parts[-1]
        module = parts[-2] if len(parts) >= 2 else "unknown"
        section_key = filename[:-3]  # strip .md
        section_label = section_key.replace("_", " ").title()
        items.append(DocumentListItem(
            gcs_path=path,
            module=module,
            section_key=section_key,
            section_label=section_label,
        ))

    return DocumentListResponse(documents=items)


@router.get("/documents/read", response_model=DocumentReadResponse)
def read_document(
    therapeutic_area: str = Query(...),
    disease_type: str = Query(...),
    drug_name: str = Query(...),
    section_key: str = Query(...),
    module: str = Query(...),
    bucket_name: str = Query(default=None),
) -> DocumentReadResponse:
    """Read the content of a specific generated section."""
    bucket = bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    from writer.gcs_client import program_prefix
    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )
    gcs_path = f"{program_prefix(program)}/templates/{module}/{section_key}.md"

    try:
        content = load_template(bucket, gcs_path)
    except Exception as exc:
        logger.error("Failed to read document %s: %s", gcs_path, exc)
        raise HTTPException(status_code=404, detail=f"Section not found: {gcs_path}") from exc

    return DocumentReadResponse(gcs_path=gcs_path, content=content)


# ── Clinical data manifest ────────────────────────────────────────────────────

class ManifestColumn(BaseModel):
    column_name: str
    role: str
    ctd_section_keys: list[str]
    placeholder_key: str


class ManifestSource(BaseModel):
    filename: str
    gcs_path: str
    columns: list[ManifestColumn]


class ManifestResponse(BaseModel):
    sources: list[ManifestSource]
    total_columns: int


@router.get("/clinical-data/manifest", response_model=ManifestResponse)
def get_clinical_manifest(
    therapeutic_area: str = Query(...),
    disease_type: str = Query(...),
    drug_name: str = Query(...),
    bucket_name: str = Query(default=None),
) -> ManifestResponse:
    """Return the clinical data column manifest for a program.

    Used by the UI to display evidence sources alongside generated content.
    Returns an empty response (not 404) if no manifest exists yet.
    """
    bucket = bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )

    from writer.gcs_client import program_prefix
    manifest_path = f"{program_prefix(program)}/clinical_data/manifest.json"

    try:
        blob = gcs().bucket(bucket).blob(manifest_path)
        if not blob.exists():
            return ManifestResponse(sources=[], total_columns=0)
        raw: dict[str, Any] = json.loads(blob.download_as_text())
    except Exception as exc:
        logger.warning("Could not load manifest %s: %s", manifest_path, exc)
        return ManifestResponse(sources=[], total_columns=0)

    sources: list[ManifestSource] = []
    total = 0
    for entry in raw.get("sources", []):
        cols = [
            ManifestColumn(
                column_name=c.get("column_name", ""),
                role=c.get("role", ""),
                ctd_section_keys=c.get("ctd_section_keys", []),
                placeholder_key=c.get("placeholder_key", ""),
            )
            for c in entry.get("columns", [])
            if c.get("column_name")
        ]
        sources.append(ManifestSource(
            filename=entry.get("filename", "unknown"),
            gcs_path=entry.get("gcs_path", ""),
            columns=cols,
        ))
        total += len(cols)

    return ManifestResponse(sources=sources, total_columns=total)
