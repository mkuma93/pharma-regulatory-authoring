"""POST /clinical-data/upload — upload a clinical CSV for a program/drug.

The file is stored in GCS under the program path and the column manifest is
auto-built (or updated) so the writer service knows which data belongs to
which drug under which program.

GCS layout after upload
───────────────────────
  {bucket}/
    therapeutic-area/{ta}/{disease}/{drug}/
      clinical_data/
        {filename}.csv          ← uploaded file
        manifest.json           ← created / updated by this endpoint

Request (multipart/form-data)
─────────────────────────────
  file               — CSV file (UploadFile)
  therapeutic_area   — e.g. "neurology"
  disease_type       — e.g. "bell's palsy"
  drug_name          — e.g. "prednisolone"
  bucket_name        — GCS bucket (optional if set in settings)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, UploadFile
from langchain_openai import ChatOpenAI

from config.settings import settings
from writer.clinical_uploader import UploadResult, upload_clinical_csv
from writer.models import ProgramInfo

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_CSV_BYTES = 50 * 1024 * 1024  # 50 MB guard


@router.post("/clinical-data/upload", response_model=UploadResult)
async def upload_clinical_data(
    file: UploadFile,
    therapeutic_area: str = Form(..., description="Therapeutic area, e.g. 'neurology'"),
    disease_type: str = Form(..., description="Disease / indication, e.g. 'bell\\'s palsy'"),
    drug_name: str = Form(..., description="Drug name, e.g. 'prednisolone'"),
    bucket_name: str = Form(default="", description="GCS bucket (falls back to service default)"),
) -> UploadResult:
    """Upload a clinical CSV and auto-map its columns to the CTD manifest.

    - The CSV is stored under `{program_prefix}/clinical_data/{filename}` in GCS.
    - The LLM mapper identifies column roles, CTD section keys, and placeholder names.
    - `manifest.json` is created or updated so the `/write` endpoint can find the data.
    """
    effective_bucket = bucket_name or settings.gcs_bucket_name
    if not effective_bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=422, detail="Only CSV files are supported.")

    csv_bytes = await file.read()
    if len(csv_bytes) > _MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {_MAX_CSV_BYTES // (1024*1024)} MB.",
        )
    if not csv_bytes:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")

    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )

    llm = ChatOpenAI(
        model=settings.llm_model,
        temperature=0.0,
        api_key=settings.openai_api_key,
    )

    try:
        result = upload_clinical_csv(
            bucket_name=effective_bucket,
            program=program,
            filename=file.filename,
            csv_bytes=csv_bytes,
            llm=llm,
        )
    except Exception as exc:
        logger.error("[upload] Failed for %s / %s: %s", drug_name, file.filename, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return result
