"""POST /validate — run cross-module consistency validation on existing documents."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from config.settings import settings
from validator.graph import run_validator
from writer.models import SectionDocument, ValidationResult

logger = logging.getLogger(__name__)
router = APIRouter()


class ValidateRequest(BaseModel):
    """Validate a list of already-written CTD section documents."""
    documents: list[SectionDocument]


class ValidateResponse(BaseModel):
    validation: ValidationResult
    sections_checked: int


@router.post("/validate", response_model=ValidateResponse)
def validate(request: ValidateRequest) -> ValidateResponse:
    """Run cross-module consistency validation on the supplied documents.

    Accepts documents that were written by a previous `/write` call (or
    uploaded manually) and returns a `ValidationResult` with any issues found.
    Does NOT write or modify documents in GCS.
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

    return ValidateResponse(
        validation=validation,
        sections_checked=len(request.documents),
    )
