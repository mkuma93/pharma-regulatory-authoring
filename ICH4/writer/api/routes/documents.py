"""GET /documents — list and read generated template documents from GCS."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config.settings import settings
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
