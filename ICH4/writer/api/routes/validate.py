"""POST /validate — run cross-module consistency validation on existing documents."""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from config.settings import settings
from validator.graph import run_validator
from writer.models import ProgramInfo, SectionDocument, ValidationResult
from writer.storage import save_validation_result

logger = logging.getLogger(__name__)
router = APIRouter()


class ValidateRequest(BaseModel):
    """Validate a list of already-written CTD section documents."""
    documents: list[SectionDocument]
    # Optional — when supplied the result is persisted to GCS
    program: ProgramInfo | None = None
    bucket_name: str | None = None
    run_id: str | None = None
    author: str | None = None


class ValidateResponse(BaseModel):
    validation: ValidationResult
    sections_checked: int
    gcs_path: str | None = None


@router.post("/validate", response_model=ValidateResponse)
def validate(request: ValidateRequest) -> ValidateResponse:
    """Run cross-module consistency validation on the supplied documents.

    Accepts documents that were written by a previous `/write` call (or
    uploaded manually) and returns a `ValidationResult` with any issues found.
    When `program` and `bucket_name` are provided the result is also persisted
    to GCS at ``validation/runs/{run_id}/report.json`` and
    ``validation/latest.json``.
    """
    if not request.documents:
        raise HTTPException(status_code=422, detail="At least one document is required.")

    llm = ChatOpenAI(
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        api_key=settings.openai_api_key,
    )

    try:
        validation = run_validator(documents=request.documents, llm=llm)
    except Exception as exc:
        logger.error("[validate] Validator error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Validator error: {exc}") from exc

    gcs_path: str | None = None
    if request.program and request.bucket_name:
        run_id = request.run_id or str(uuid.uuid4())
        try:
            gcs_path = save_validation_result(
                bucket_name=request.bucket_name,
                program=request.program,
                validation=validation,
                run_id=run_id,
                author=request.author or "system",
                section_keys=[d.section_key for d in request.documents],
            )
        except Exception as exc:
            logger.warning("[validate] Failed to persist validation result: %s", exc)

    return ValidateResponse(
        validation=validation,
        sections_checked=len(request.documents),
        gcs_path=gcs_path,
    )
