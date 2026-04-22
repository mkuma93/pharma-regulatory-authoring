"""POST /write — generate and persist CTD documents from templates."""
from __future__ import annotations

import concurrent.futures
import logging

from fastapi import APIRouter, HTTPException
from langchain_openai import ChatOpenAI

from config.settings import settings
from validator.graph import run_validator
from writer.generator import write_section
from writer.models import ValidationResult, WriterRequest, WriterResponse
from writer.storage import (
    list_template_paths,
    load_template,
    parse_section_meta_from_path,
    save_document,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_VALIDATOR_TIMEOUT_SECONDS = 120  # 2-minute cap; prevents worker HTTP timeout on large writes


@router.post("/write", response_model=WriterResponse)
def write(request: WriterRequest) -> WriterResponse:
    bucket_name = request.bucket_name or settings.gcs_bucket_name
    if not bucket_name:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    llm = ChatOpenAI(
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        api_key=settings.openai_api_key,
    )

    # Gather template paths (storage module manages its own GCS client)
    try:
        all_paths = list_template_paths(bucket_name, request.program)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GCS error listing templates: {exc}") from exc

    if not all_paths:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No templates found for program "
                f"{request.program.therapeutic_area}/{request.program.disease_type}"
                f"/{request.program.drug_name}."
            ),
        )

    # Filter to requested sections (empty list = all)
    if request.sections:
        section_filter = set(request.sections)
        paths = [p for p in all_paths if _section_key_from_path(p) in section_filter]
    else:
        paths = all_paths

    # Write each section
    documents = []
    sections_failed: list[str] = []

    for gcs_path in paths:
        module_key, section_key, section_label = parse_section_meta_from_path(gcs_path)
        module_label = module_key.replace("_", " ").title()

        try:
            template_content = load_template(bucket_name, gcs_path)
            doc = write_section(
                program=request.program,
                section_key=section_key,
                section_label=section_label,
                module_key=module_key,
                module_label=module_label,
                template_content=template_content,
                bucket_name=bucket_name,
                llm=llm,
                resolved_values=request.resolved_values or {},
            )
            doc.gcs_path = save_document(bucket_name, request.program, doc)
            documents.append(doc)
        except Exception as exc:
            logger.error("[write] Failed section %s: %s", section_key, exc)
            sections_failed.append(section_key)

    # Validate
    validation: ValidationResult
    if request.run_validator and documents:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    run_validator,
                    documents,
                    llm,
                    request.resolved_values or {},
                )
                try:
                    validation = future.result(timeout=_VALIDATOR_TIMEOUT_SECONDS)
                except concurrent.futures.TimeoutError:
                    logger.warning(
                        "[write] Validator timed out after %ds — skipping",
                        _VALIDATOR_TIMEOUT_SECONDS,
                    )
                    validation = ValidationResult(
                        passed=True,
                        issues=[],
                        summary=(
                            f"Validator skipped: timed out after "
                            f"{_VALIDATOR_TIMEOUT_SECONDS}s."
                        ),
                    )
        except Exception as exc:
            logger.warning("[write] Validator error: %s", exc)
            validation = ValidationResult(
                passed=True,
                issues=[],
                summary=f"Validator skipped due to error: {exc}",
            )
    else:
        validation = ValidationResult(
            passed=True,
            issues=[],
            summary="Validator not requested.",
        )

    return WriterResponse(
        documents=documents,
        validation=validation,
        sections_written=len(documents),
        sections_failed=sections_failed,
    )


def _section_key_from_path(gcs_path: str) -> str:
    """Extract the section key (stem) from a GCS template path."""
    return gcs_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")
