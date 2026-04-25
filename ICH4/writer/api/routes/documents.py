"""GET /documents — list and read generated documents from GCS.
GET /clinical-data/manifest — return clinical column mapping for a program.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

try:
    from iap_identity import resolve_author  # injected via Dockerfile COPY
except ImportError:  # pragma: no cover — local dev fallback
    def resolve_author(body_author: str, headers) -> str:  # noqa: E704
        return (body_author or "").strip()

from config.settings import settings
from writer.gcs_client import gcs
from writer.models import ProgramInfo
from writer.storage import (
    GATE_ROLES_ALL,
    GATE_ROLES_REQUIRED,
    ROLE_LABELS,
    admin_seed_publish_status,
    list_generated_paths,
    load_gate_status,
    load_publish_status,
    load_template,
    record_section_approval,
    record_section_rejection,
    save_document_edit,
    save_template,
)

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
    publish_status: dict | None = None


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
        paths = list_generated_paths(bucket, program)
    except Exception as exc:
        logger.error("Failed to list documents: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    items: list[DocumentListItem] = []
    for path in sorted(paths):
        # path format: …/ctd/{module}/{section_key}/document.md
        parts = path.rstrip("/").split("/")
        # parts[-1] == "document.md", parts[-2] == section_key, parts[-3] == module
        if len(parts) < 3 or parts[-1] != "document.md":
            continue
        section_key = parts[-2]
        module      = parts[-3]
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
    gcs_path = f"{program_prefix(program)}/ctd/{module}/{section_key}/document.md"

    try:
        content = load_template(bucket, gcs_path)
    except Exception as exc:
        logger.error("Failed to read document %s: %s", gcs_path, exc)
        raise HTTPException(status_code=404, detail=f"Section not found: {gcs_path}") from exc

    publish_status = load_publish_status(bucket, program, module, section_key)
    return DocumentReadResponse(
        gcs_path=gcs_path,
        content=content,
        publish_status=publish_status,
    )


# ── Version history ───────────────────────────────────────────────────────────

class VersionEntry(BaseModel):
    version: int
    timestamp: str
    run_id: str
    author: str
    gcs_path: str


class VersionListResponse(BaseModel):
    section_key: str
    versions: list[VersionEntry]


class VersionReadResponse(BaseModel):
    section_key: str
    version: int
    author: str
    timestamp: str
    content: str


@router.get("/documents/versions", response_model=VersionListResponse)
def list_versions(
    therapeutic_area: str = Query(...),
    disease_type: str = Query(...),
    drug_name: str = Query(...),
    section_key: str = Query(...),
    module: str = Query(...),
    bucket_name: str = Query(default=None),
) -> VersionListResponse:
    """List all prior versions of a CTD section, newest first."""
    bucket = bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    from writer.gcs_client import program_prefix
    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )
    vprefix = (
        f"{program_prefix(program)}/ctd/{module}/{section_key}/versions"
    )
    manifest_blob = gcs().bucket(bucket).blob(f"{vprefix}/manifest.json")
    try:
        if not manifest_blob.exists():
            return VersionListResponse(section_key=section_key, versions=[])
        entries: list[dict] = json.loads(manifest_blob.download_as_text())
    except Exception as exc:
        logger.warning("Could not load version manifest for %s: %s", section_key, exc)
        return VersionListResponse(section_key=section_key, versions=[])

    versions = [
        VersionEntry(
            version=e.get("version", 0),
            timestamp=e.get("timestamp", ""),
            run_id=e.get("run_id", ""),
            author=e.get("author", "system"),
            gcs_path=e.get("gcs_path", ""),
        )
        for e in entries
    ]
    versions.sort(key=lambda v: v.version, reverse=True)
    return VersionListResponse(section_key=section_key, versions=versions)


@router.get("/documents/version", response_model=VersionReadResponse)
def read_version(
    therapeutic_area: str = Query(...),
    disease_type: str = Query(...),
    drug_name: str = Query(...),
    section_key: str = Query(...),
    module: str = Query(...),
    version: int = Query(...),
    bucket_name: str = Query(default=None),
) -> VersionReadResponse:
    """Read the content of a specific prior version of a CTD section."""
    bucket = bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    from writer.gcs_client import program_prefix
    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )
    vprefix      = f"{program_prefix(program)}/ctd/{module}/{section_key}/versions"
    version_path = f"{vprefix}/v{version}.md"

    try:
        content = load_template(bucket, version_path)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version} of {section_key} not found",
        ) from exc

    # Read author/timestamp from manifest
    author = "system"
    timestamp = ""
    try:
        manifest_blob = gcs().bucket(bucket).blob(f"{vprefix}/manifest.json")
        if manifest_blob.exists():
            entries: list[dict] = json.loads(manifest_blob.download_as_text())
            for e in entries:
                if e.get("version") == version:
                    author    = e.get("author", "system")
                    timestamp = e.get("timestamp", "")
                    break
    except Exception:
        pass

    return VersionReadResponse(
        section_key=section_key,
        version=version,
        author=author,
        timestamp=timestamp,
        content=content,
    )


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


# ── Publish status / validation report / human approval (A1 + A6) ────────────

class PublishStatusResponse(BaseModel):
    section_key: str
    status: dict | None


# ── Admin: seed publish_status for sections without a validator pass ──────────

class AdminSeedRequest(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str
    module: str
    section_key: str
    admin_email: str = Field(..., description="Admin email; included verbatim in the audit trail.")
    seed_reason: str = Field(..., description="Reason the admin is seeding this record (min 5 chars).")
    bucket_name: str | None = None


class AdminSeedResponse(BaseModel):
    seeded: bool
    status: dict


@router.post("/documents/admin/seed-publish-status", response_model=AdminSeedResponse)
def admin_seed_section(req: AdminSeedRequest, http_request: Request) -> AdminSeedResponse:
    """Admin gate: manually create a publish_status.json for a section that was
    generated by the content pipeline before the /write validator existed.

    Access control: the caller must use the ``admin`` persona (email contains
    ``@demo.com`` in dev, or a validated admin group in prod via IAP).  This
    endpoint is intentionally separate from the normal reviewer flow — every
    call is permanently logged with the admin's identity and stated reason.

    The seeded record sets ``publish_approved=True`` so reviewers can then
    sign their gates normally.  It does NOT grant any gate approval itself.
    """
    bucket = req.bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    # Resolve admin identity from IAP header if not provided in body.
    admin_email = resolve_author(req.admin_email, http_request.headers)
    if not admin_email:
        raise HTTPException(status_code=422, detail="admin_email is required.")

    program = ProgramInfo(
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    try:
        status = admin_seed_publish_status(
            bucket_name=bucket,
            program=program,
            module_key=req.module,
            section_key=req.section_key,
            admin_email=admin_email,
            seed_reason=req.seed_reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Admin seed failed: {exc}") from exc

    return AdminSeedResponse(seeded=True, status=status)


@router.get("/documents/publish_status", response_model=PublishStatusResponse)
def get_publish_status(
    therapeutic_area: str = Query(...),
    disease_type: str = Query(...),
    drug_name: str = Query(...),
    section_key: str = Query(...),
    module: str = Query(...),
    bucket_name: str = Query(default=None),
) -> PublishStatusResponse:
    """Return the publish_status.json sidecar for a section (or null if none)."""
    bucket = bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")
    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )
    status = load_publish_status(bucket, program, module, section_key)
    return PublishStatusResponse(section_key=section_key, status=status)


class ValidationReportResponse(BaseModel):
    gcs_path: str
    report: dict


@router.get("/documents/validation/latest", response_model=ValidationReportResponse)
def get_latest_validation(
    therapeutic_area: str = Query(...),
    disease_type: str = Query(...),
    drug_name: str = Query(...),
    bucket_name: str = Query(default=None),
) -> ValidationReportResponse:
    """Return the most recent ValidationResult for a program."""
    bucket = bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")

    from writer.gcs_client import program_prefix
    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )
    path = f"{program_prefix(program)}/validation/latest.json"
    blob = gcs().bucket(bucket).blob(path)
    try:
        if not blob.exists():
            raise HTTPException(status_code=404, detail="No validation report yet.")
        report = json.loads(blob.download_as_text())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load report: {exc}") from exc
    return ValidationReportResponse(gcs_path=path, report=report)


class ApproveRequest(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str
    section_key: str
    module: str
    approver: str = Field(..., description="Email of approver; must differ from author.")
    approval_reason: str = Field(
        ...,
        min_length=3,
        description="Required audit-trail reason for the approval.",
    )
    role: str = Field(
        default="qa_compliance",
        description=(
            "Reviewer role — one of: statistician, medical_writer, qa_compliance, "
            "clinical_lead, regulatory, pharmacovigilance."
        ),
    )
    run_id: str = ""
    bucket_name: str | None = None


class ApproveResponse(BaseModel):
    approved: bool
    status: dict


@router.post("/documents/approve", response_model=ApproveResponse)
def approve_section(req: ApproveRequest, http_request: Request) -> ApproveResponse:
    """Human approval gate — enforces approver ≠ author (A6 segregation of duties).

    Preconditions:
      - publish_status.json exists for this section (i.e. /write ran the validator).
      - validator did not block (publish_approved=True in sidecar).
      - ``approver`` email differs from the original ``author``.

    On success, the publish_status sidecar is flipped to ``human_approved=True``
    and an immutable ``approvals/…json`` record is written with witness metadata.
    """
    bucket = req.bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")
    # A2 — IAP identity: if the caller omitted ``approver`` in the body, use
    # the IAP-authenticated user email. Regulators expect the approver to be a
    # real human, never the service account.
    req.approver = resolve_author(req.approver, http_request.headers)
    if not req.approver:
        raise HTTPException(
            status_code=422,
            detail="approver is required (body field or IAP header).",
        )
    if not req.approval_reason.strip():
        raise HTTPException(
            status_code=422,
            detail="approval_reason is required (audit trail).",
        )
    if req.role not in GATE_ROLES_ALL:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown role '{req.role}'. Allowed: {', '.join(GATE_ROLES_ALL)}.",
        )
    program = ProgramInfo(
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    try:
        status = record_section_approval(
            bucket_name=bucket,
            program=program,
            module_key=req.module,
            section_key=req.section_key,
            approver=req.approver,
            approval_reason=req.approval_reason,
            run_id=req.run_id,
            role=req.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Approval failed: {exc}") from exc
    return ApproveResponse(approved=True, status=status)


class RejectRequest(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str
    section_key: str
    module: str
    reviewer: str = Field(..., description="Email of reviewer; must differ from author.")
    rejection_reason: str = Field(
        ...,
        min_length=3,
        description="Required audit-trail reason for the rejection.",
    )
    role: str = Field(
        default="qa_compliance",
        description=(
            "Reviewer role — one of: statistician, medical_writer, qa_compliance, "
            "clinical_lead, regulatory, pharmacovigilance."
        ),
    )
    run_id: str = ""
    bucket_name: str | None = None


class RejectResponse(BaseModel):
    rejected: bool
    status: dict


@router.post("/documents/reject", response_model=RejectResponse)
def reject_section(req: RejectRequest, http_request: Request) -> RejectResponse:
    """Reviewer rejection gate — clears all approvals and blocks release.

    Preconditions:
      - publish_status.json exists for this section.
      - ``reviewer`` email differs from the original ``author``.

    On rejection, all gate approvals are cleared and the section is marked
    publish-blocked until the author revises and re-generates.
    """
    bucket = req.bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")
    req.reviewer = resolve_author(req.reviewer, http_request.headers)
    if not req.reviewer:
        raise HTTPException(
            status_code=422,
            detail="reviewer is required (body field or IAP header).",
        )
    if not req.rejection_reason.strip():
        raise HTTPException(
            status_code=422,
            detail="rejection_reason is required (audit trail).",
        )
    if req.role not in GATE_ROLES_ALL:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown role '{req.role}'. Allowed: {', '.join(GATE_ROLES_ALL)}.",
        )
    program = ProgramInfo(
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    try:
        status = record_section_rejection(
            bucket_name=bucket,
            program=program,
            module_key=req.module,
            section_key=req.section_key,
            reviewer=req.reviewer,
            rejection_reason=req.rejection_reason,
            run_id=req.run_id,
            role=req.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rejection failed: {exc}") from exc
    return RejectResponse(rejected=True, status=status)


class GateStatusResponse(BaseModel):
    module_key:       str
    section_key:      str
    gcs_path:         str
    current_version:  int | None = None
    author:           str = ""
    publish_approved: bool = False
    publish_blocked_reason: str = ""
    required_roles:   list[str]
    role_labels:      dict[str, str]
    gates:            dict[str, dict]
    release_ready:    bool
    validation_gcs_path: str = ""


@router.get("/documents/gate-status", response_model=GateStatusResponse)
def get_gate_status(
    therapeutic_area: str = Query(...),
    disease_type:     str = Query(...),
    drug_name:        str = Query(...),
    module:           str = Query(...),
    section_key:      str = Query(...),
    bucket_name:      str = Query(default=None),
) -> GateStatusResponse:
    """Rollup of the multi-reviewer validation-package gates for one section."""
    bucket = bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")
    program = ProgramInfo(
        therapeutic_area=therapeutic_area,
        disease_type=disease_type,
        drug_name=drug_name,
    )
    try:
        data = load_gate_status(bucket, program, module, section_key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return GateStatusResponse(**data)


# ── Manual edits: generated content + templates ──────────────────────────────

class EditDocumentRequest(BaseModel):
    therapeutic_area: str
    disease_type:     str
    drug_name:        str
    module:           str
    section_key:      str
    content:          str = Field(..., description="Full replacement markdown.")
    author:           str = ""
    edit_reason:      str = Field(
        ...,
        min_length=3,
        description="Required audit-trail reason for the edit (min 3 chars).",
    )
    run_id:           str = ""
    bucket_name:      str | None = None


class EditResponse(BaseModel):
    gcs_path:    str
    version:     int | None = None
    author:      str
    timestamp:   str
    edit_reason: str = ""


@router.put("/documents/content", response_model=EditResponse)
def edit_document_content(
    req: EditDocumentRequest,
    http_request: Request,
) -> EditResponse:
    """Save a user-edited CTD section.

    The previous ``document.md`` is snapshotted into ``versions/v{N}.md`` and
    the edit is attributed to the IAP-authenticated user (falling back to
    ``req.author`` when running outside Cloud Run).
    """
    bucket = req.bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")
    author = resolve_author(req.author, http_request.headers)
    if not author:
        raise HTTPException(
            status_code=422,
            detail="author is required (body field or IAP header).",
        )
    if not req.edit_reason.strip():
        raise HTTPException(
            status_code=422,
            detail="edit_reason is required (audit trail).",
        )
    program = ProgramInfo(
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    try:
        result = save_document_edit(
            bucket_name=bucket,
            program=program,
            module_key=req.module,
            section_key=req.section_key,
            content=req.content,
            author=author,
            edit_reason=req.edit_reason,
            run_id=req.run_id,
        )
    except Exception as exc:
        logger.error("Document edit failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Edit failed: {exc}") from exc
    return EditResponse(**result)


class TemplateReadResponse(BaseModel):
    gcs_path: str
    content:  str


@router.get("/templates/read", response_model=TemplateReadResponse)
def read_template(
    therapeutic_area: str = Query(...),
    disease_type:     str = Query(...),
    drug_name:        str = Query(...),
    module:           str = Query(...),
    section_key:      str = Query(...),
    bucket_name:      str = Query(default=None),
) -> TemplateReadResponse:
    """Return the current source of a program's section template."""
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
        raise HTTPException(status_code=404, detail=f"Template not found: {gcs_path}") from exc
    return TemplateReadResponse(gcs_path=gcs_path, content=content)


@router.put("/templates/content", response_model=EditResponse)
def edit_template_content(
    req: EditDocumentRequest,
    http_request: Request,
) -> EditResponse:
    """Save an edited template and snapshot the previous revision."""
    bucket = req.bucket_name or settings.gcs_bucket_name
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket_name is required.")
    author = resolve_author(req.author, http_request.headers)
    if not author:
        raise HTTPException(
            status_code=422,
            detail="author is required (body field or IAP header).",
        )
    if not req.edit_reason.strip():
        raise HTTPException(
            status_code=422,
            detail="edit_reason is required (audit trail).",
        )
    program = ProgramInfo(
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    try:
        result = save_template(
            bucket_name=bucket,
            program=program,
            module_key=req.module,
            section_key=req.section_key,
            content=req.content,
            author=author,
            edit_reason=req.edit_reason,
        )
    except Exception as exc:
        logger.error("Template edit failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Template edit failed: {exc}") from exc
    return EditResponse(**result)
