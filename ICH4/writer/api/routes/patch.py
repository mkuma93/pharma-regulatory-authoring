"""POST /patch — re-write CTD sections affected by clinical data schema changes.

Patch mode differs from /write in that:
  • It loads the existing generated document from GCS as prior context.
  • The LLM is instructed to keep unchanged prose verbatim and only revise
    sentences / tables that reference the ``changed_keys`` placeholders.
  • The caller (content_worker /schema-patch) passes the list of placeholder
    keys that changed so the LLM knows exactly what needs updating.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

try:
    from iap_identity import resolve_author  # injected via Dockerfile COPY
except ImportError:  # pragma: no cover — local dev fallback
    def resolve_author(body_author: str, headers) -> str:  # noqa: E704
        return (body_author or "").strip()

from config.settings import settings
from writer.generator import write_section
from writer.gcs_client import gcs, program_prefix
from writer.models import ProgramInfo, ValidationResult, WriterResponse
from writer.storage import (
    list_template_paths,
    load_template,
    parse_section_meta_from_path,
    save_document,
)
from validator.graph import run_validator

logger = logging.getLogger(__name__)
router = APIRouter()


class PatchRequest(BaseModel):
    program: ProgramInfo
    bucket_name: str | None = Field(default=None)
    run_id: str = Field(default="", description="Content-generation run identifier for traceability.")
    author: str = Field(default="", description="User who triggered the patch (IAP email).")
    sections: list[str] = Field(
        default_factory=list,
        description="CTD section keys to patch. Empty = patch ALL sections that have a generated document.",
    )
    changed_keys: list[str] = Field(
        default_factory=list,
        description="Placeholder keys that changed in the updated clinical dataset.",
    )
    resolved_values: dict[str, str] = Field(default_factory=dict)
    run_validator: bool = Field(default=False)


def _load_existing_document(bucket_name: str, program: ProgramInfo, module_key: str, section_key: str) -> str | None:
    """Load a previously generated document from GCS, or return None if absent."""
    prefix   = program_prefix(program)
    gcs_path = f"{prefix}/ctd/{module_key}/{section_key}/document.md"
    try:
        blob = gcs().bucket(bucket_name).blob(gcs_path)
        if blob.exists():
            return blob.download_as_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("[patch] Could not load existing document %s: %s", gcs_path, exc)
    return None


def _section_key_from_path(gcs_path: str) -> str:
    return gcs_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")


@router.post("/patch", response_model=WriterResponse)
def patch(request: PatchRequest, http_request: Request) -> WriterResponse:
    """Patch existing CTD sections after a clinical data schema change.

    For each section, the call loads the previously generated document and
    passes it to the writer as ``prior_content``.  The writer LLM is then
    instructed to keep all unchanged prose verbatim and only revise sentences
    referencing the placeholders listed in ``changed_keys``.

    If no prior document is found for a section (e.g. it was never written)
    the section falls back to a full write so no content is silently skipped.
    """
    bucket_name = request.bucket_name or settings.gcs_bucket_name
    if not bucket_name:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    # A2 — IAP identity fallback.
    request.author = resolve_author(request.author, http_request.headers)

    llm = ChatOpenAI(
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        api_key=settings.openai_api_key,
    )

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

    if request.sections:
        section_filter = set(request.sections)
        paths = [p for p in all_paths if _section_key_from_path(p) in section_filter]
    else:
        paths = all_paths

    documents = []
    sections_failed: list[str] = []

    for gcs_path in paths:
        module_key, section_key, section_label = parse_section_meta_from_path(gcs_path)
        module_label = module_key.replace("_", " ").title()

        try:
            template_content = load_template(bucket_name, gcs_path)
            prior_content    = _load_existing_document(
                bucket_name, request.program, module_key, section_key
            )
            if prior_content:
                logger.info("[patch] Section %s — patching existing document", section_key)
            else:
                logger.info("[patch] Section %s — no prior document; falling back to full write", section_key)

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
                prior_content=prior_content,
                changed_keys=request.changed_keys or None,
            )
            doc.gcs_path = save_document(bucket_name, request.program, doc,
                                          run_id=request.run_id, author=request.author)
            documents.append(doc)
        except Exception as exc:
            logger.error("[patch] Failed section %s: %s", section_key, exc)
            sections_failed.append(section_key)

    validation: ValidationResult
    if request.run_validator and documents:
        try:
            validation = run_validator(
                documents=documents,
                llm=llm,
                resolved_values=request.resolved_values or {},
            )
        except Exception as exc:
            logger.warning("[patch] Validator error: %s", exc)
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
