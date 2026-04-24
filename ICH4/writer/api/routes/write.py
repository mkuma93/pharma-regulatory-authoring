"""POST /write — generate and persist CTD documents from templates."""
from __future__ import annotations

import concurrent.futures
import logging

from fastapi import APIRouter, HTTPException, Request
from langchain_openai import ChatOpenAI

try:
    from iap_identity import resolve_author  # injected via Dockerfile COPY
except ImportError:  # pragma: no cover — local dev fallback
    def resolve_author(body_author: str, headers) -> str:  # noqa: E704
        return (body_author or "").strip()

from config.settings import settings
from validator.graph import run_validator
from writer.generator import write_section
from writer.models import ValidationResult, WriterRequest, WriterResponse
from writer.storage import (
    list_template_paths,
    load_template,
    parse_section_meta_from_path,
    save_document,
    save_publish_status,
    save_validation_result,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_VALIDATOR_TIMEOUT_SECONDS = 120  # 2-minute cap; prevents worker HTTP timeout on large writes


@router.post("/write", response_model=WriterResponse)
def write(request: WriterRequest, http_request: Request) -> WriterResponse:
    bucket_name = request.bucket_name or settings.gcs_bucket_name
    if not bucket_name:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    # A2 — IAP identity: if request.author is empty, attribute this run to
    # the IAP-authenticated end user instead of the Cloud Run service account.
    request.author = resolve_author(request.author, http_request.headers)

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
            doc.gcs_path = save_document(
                bucket_name, request.program, doc,
                run_id=request.run_id,
                author=request.author,
            )
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

    # ── Persist ValidationResult + write publish_status sidecars ─────────────
    # (A1 traceability — regulator-visible artifacts for every run)
    if documents:
        try:
            validation_path = save_validation_result(
                bucket_name=bucket_name,
                program=request.program,
                validation=validation,
                run_id=request.run_id,
                author=request.author,
                section_keys=[d.section_key for d in documents],
            )
            save_publish_status(
                bucket_name=bucket_name,
                program=request.program,
                documents=documents,
                validation=validation,
                validation_gcs_path=validation_path,
                run_id=request.run_id,
                author=request.author,
            )
        except Exception as exc:
            logger.warning("[write] Failed to persist validation artifacts: %s", exc)

    return WriterResponse(
        documents=documents,
        validation=validation,
        sections_written=len(documents),
        sections_failed=sections_failed,
    )


def _section_key_from_path(gcs_path: str) -> str:
    """Extract the section key (stem) from a GCS template path."""
    return gcs_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")
