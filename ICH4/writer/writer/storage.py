"""GCS helpers for loading templates and saving final documents.

Versioning layout per section:
  .../ctd/{module_key}/{section_key}/
    document.md          ← always the latest
    versions/
      v1.md              ← first-ever write
      v2.md              ← second write, etc.
      manifest.json      ← [{version, timestamp, run_id, author, gcs_path}, ...]
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .gcs_client import gcs, program_prefix
from .models import ProgramInfo, SectionDocument, ValidationResult

try:
    from bq_audit import emit_document_version  # injected via Dockerfile COPY
except ImportError:
    def emit_document_version(**_): pass  # noqa: E704 — no-op when running locally

try:
    from bq_audit import (  # noqa: E501
        emit_validation_persisted,
        emit_publish_status,
        emit_section_approved,
    )
except ImportError:
    def emit_validation_persisted(**_): pass   # noqa: E704
    def emit_publish_status(**_): pass         # noqa: E704
    def emit_section_approved(**_): pass       # noqa: E704

logger = logging.getLogger(__name__)


def list_template_paths(bucket_name: str, program: ProgramInfo) -> list[str]:
    """Return all approved template .md paths under the program's templates/ prefix."""
    prefix = f"{program_prefix(program)}/templates/"
    bkt    = gcs().bucket(bucket_name)
    return [b.name for b in bkt.list_blobs(prefix=prefix) if b.name.endswith(".md")]


def list_generated_paths(bucket_name: str, program: ProgramInfo) -> list[str]:
    """Return all generated document.md paths under the program's ctd/ prefix.

    Scans therapeutic-area/{ta}/{dis}/{drug}/ctd/{module}/{section}/document.md
    and returns the full GCS blob names for all that exist.
    """
    prefix = f"{program_prefix(program)}/ctd/"
    bkt    = gcs().bucket(bucket_name)
    return [
        b.name for b in bkt.list_blobs(prefix=prefix)
        if b.name.endswith("/document.md")
    ]


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


# ── Version helpers ───────────────────────────────────────────────────────────

def _versions_prefix(prefix: str, module_key: str, section_key: str) -> str:
    return f"{prefix}/ctd/{module_key}/{section_key}/versions"


def _load_version_manifest(bkt, vprefix: str) -> list[dict]:
    """Return existing version manifest list, or [] if none exists."""
    blob = bkt.blob(f"{vprefix}/manifest.json")
    try:
        if blob.exists():
            return json.loads(blob.download_as_text())
    except Exception as exc:
        logger.warning("[storage] Could not load version manifest: %s", exc)
    return []


def _save_version_manifest(bkt, vprefix: str, manifest: list[dict]) -> None:
    bkt.blob(f"{vprefix}/manifest.json").upload_from_string(
        json.dumps(manifest, indent=2),
        content_type="application/json",
    )


def save_document(
    bucket_name: str,
    program: ProgramInfo,
    doc: SectionDocument,
    run_id: str = "",
    author: str = "",
) -> str:
    """Write a completed document to GCS with version history.

    Before overwriting document.md the existing blob (if any) is copied to
    versions/v{N}.md and recorded in versions/manifest.json.

    Args:
        run_id:  Content-generation run identifier (for traceability).
        author:  User who triggered the write, extracted from IAP header.

    Returns the GCS path of the new document.md.
    """
    prefix   = program_prefix(program)
    gcs_path = f"{prefix}/ctd/{doc.module_key}/{doc.section_key}/document.md"
    vprefix  = _versions_prefix(prefix, doc.module_key, doc.section_key)
    bkt      = gcs().bucket(bucket_name)

    # ── Snapshot current document into versions/ before overwriting ───────────
    current_blob = bkt.blob(gcs_path)
    _first_write = True
    try:
        if current_blob.exists():
            _first_write = False
            manifest = _load_version_manifest(bkt, vprefix)
            next_version = len(manifest) + 1
            versioned_path = f"{vprefix}/v{next_version}.md"
            bkt.copy_blob(current_blob, bkt, versioned_path)

            # Save the prompt that produced this version alongside the snapshot
            prompt_path = ""
            if doc.prompt_messages:
                prompt_path = f"{vprefix}/v{next_version}_prompt.json"
                bkt.blob(prompt_path).upload_from_string(
                    json.dumps({
                        "version":   next_version,
                        "run_id":    run_id,
                        "author":    author or "system",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "messages":  doc.prompt_messages,
                    }, indent=2),
                    content_type="application/json",
                )

            manifest.append({
                "version":     next_version,
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "run_id":      run_id,
                "author":      author,
                "gcs_path":    versioned_path,
                "prompt_path": prompt_path,
            })
            _save_version_manifest(bkt, vprefix, manifest)
            emit_document_version(
                run_id=run_id,
                author=author or "system",
                therapeutic_area=program.therapeutic_area,
                disease_type=program.disease_type,
                drug_name=program.drug_name,
                module_key=doc.module_key,
                section_key=doc.section_key,
                version=next_version,
                gcs_path=versioned_path,
            )
            logger.info(
                "[storage] Snapshotted v%d → gs://%s/%s (author=%s)",
                next_version, bucket_name, versioned_path, author or "system",
            )
    except Exception as exc:
        # Non-fatal — proceed with write even if versioning fails
        _first_write = False  # unknown state — suppress version=1 emit to avoid false audit
        logger.warning("[storage] Versioning failed (non-fatal): %s", exc)

    # ── Write latest ──────────────────────────────────────────────────────────
    bkt.blob(gcs_path).upload_from_string(
        doc.content,
        content_type="text/markdown; charset=utf-8",
    )
    logger.info("[storage] Saved document to gs://%s/%s", bucket_name, gcs_path)

    # Emit version=1 audit event for brand-new documents (no prior blob existed)
    if _first_write:
        emit_document_version(
            run_id=run_id,
            author=author or "system",
            therapeutic_area=program.therapeutic_area,
            disease_type=program.disease_type,
            drug_name=program.drug_name,
            module_key=doc.module_key,
            section_key=doc.section_key,
            version=1,
            gcs_path=gcs_path,
        )

    return gcs_path


# ── Validation result + publish gate ─────────────────────────────────────────

def _validation_run_path(prefix: str, run_id: str) -> str:
    safe_run = run_id or "unknown"
    return f"{prefix}/validation/runs/{safe_run}/report.json"


def _validation_latest_path(prefix: str) -> str:
    return f"{prefix}/validation/latest.json"


def _publish_status_path(prefix: str, module_key: str, section_key: str) -> str:
    return f"{prefix}/ctd/{module_key}/{section_key}/publish_status.json"


def save_validation_result(
    bucket_name: str,
    program: ProgramInfo,
    validation: ValidationResult,
    run_id: str,
    author: str,
    section_keys: list[str],
) -> str:
    """Persist a `ValidationResult` to GCS as an immutable per-run artifact.

    Writes two blobs:
      - validation/runs/{run_id}/report.json   (append-only per run)
      - validation/latest.json                 (mirror for UI convenience)

    Returns the immutable per-run path.
    """
    prefix   = program_prefix(program)
    bkt      = gcs().bucket(bucket_name)
    run_path = _validation_run_path(prefix, run_id)

    payload = {
        "run_id":           run_id,
        "author":           author or "system",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "therapeutic_area": program.therapeutic_area,
        "disease_type":     program.disease_type,
        "drug_name":        program.drug_name,
        "section_keys":     section_keys,
        "passed":           validation.passed,
        "summary":          validation.summary,
        "issues": [
            {
                "severity":    iss.severity,
                "section_key": iss.section_key,
                "message":     iss.message,
            }
            for iss in validation.issues
        ],
    }

    body = json.dumps(payload, indent=2)
    bkt.blob(run_path).upload_from_string(body, content_type="application/json")
    bkt.blob(_validation_latest_path(prefix)).upload_from_string(
        body, content_type="application/json",
    )

    emit_validation_persisted(
        run_id=run_id,
        author=author or "system",
        therapeutic_area=program.therapeutic_area,
        disease_type=program.disease_type,
        drug_name=program.drug_name,
        gcs_path=run_path,
        validation_passed=validation.passed,
        validation_issue_count=len(validation.issues),
    )
    logger.info(
        "[storage] Validation report saved gs://%s/%s (passed=%s issues=%d)",
        bucket_name, run_path, validation.passed, len(validation.issues),
    )
    return run_path


def save_publish_status(
    bucket_name: str,
    program: ProgramInfo,
    documents: list[SectionDocument],
    validation: ValidationResult,
    validation_gcs_path: str,
    run_id: str,
    author: str,
) -> dict[str, dict]:
    """Write a publish_status.json sidecar per section based on validator output.

    Rules for the demo publish gate:
      - A section is `approved=False` (blocked) if:
          * the overall validation failed AND the section has any error issue, OR
          * the section has any ``severity=="error"`` issue.
      - A section is `approved=True` otherwise (warnings/info allowed).
      - Approval-by-human (A6) later flips `approved=True` via /approve endpoint
        only when ``reviewer_approved`` is present and approver != author.

    Returns a dict ``{section_key: publish_status_dict}``.
    """
    prefix = program_prefix(program)
    bkt    = gcs().bucket(bucket_name)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Group issues by section for per-section reasoning
    issues_by_section: dict[str, list[dict]] = {}
    for iss in validation.issues:
        issues_by_section.setdefault(iss.section_key, []).append({
            "severity":    iss.severity,
            "message":     iss.message,
        })

    results: dict[str, dict] = {}
    for doc in documents:
        section_issues = issues_by_section.get(doc.section_key, [])
        has_error = any(i["severity"] == "error" for i in section_issues)
        approved  = (validation.passed or not has_error) and not has_error
        blocked_reason = ""
        if not approved:
            error_msgs = [i["message"] for i in section_issues if i["severity"] == "error"]
            blocked_reason = "; ".join(error_msgs[:3]) or "validator reported errors"

        status = {
            "run_id":                run_id,
            "author":                author or "system",
            "timestamp":             now_iso,
            "module_key":            doc.module_key,
            "section_key":           doc.section_key,
            "gcs_path":              doc.gcs_path,
            "validation_gcs_path":   validation_gcs_path,
            "validation_passed":     validation.passed,
            "section_issues":        section_issues,
            "publish_approved":      approved,
            "publish_blocked_reason": blocked_reason,
            # Human approval (A6): filled by /approve endpoint
            "human_approved":        False,
            "approver":              "",
            "approved_at":           "",
            "approval_reason":       "",
        }
        status_path = _publish_status_path(prefix, doc.module_key, doc.section_key)
        bkt.blob(status_path).upload_from_string(
            json.dumps(status, indent=2),
            content_type="application/json",
        )
        emit_publish_status(
            run_id=run_id,
            author=author or "system",
            therapeutic_area=program.therapeutic_area,
            disease_type=program.disease_type,
            drug_name=program.drug_name,
            module_key=doc.module_key,
            section_key=doc.section_key,
            gcs_path=doc.gcs_path,
            publish_approved=approved,
            publish_blocked_reason=blocked_reason,
        )
        results[doc.section_key] = status
        logger.info(
            "[storage] Publish status %s: %s (reason=%s)",
            doc.section_key,
            "APPROVED" if approved else "BLOCKED",
            blocked_reason or "-",
        )
    return results


def load_publish_status(
    bucket_name: str,
    program: ProgramInfo,
    module_key: str,
    section_key: str,
) -> dict | None:
    """Return the publish_status.json dict for a section, or None if missing."""
    prefix = program_prefix(program)
    bkt    = gcs().bucket(bucket_name)
    blob   = bkt.blob(_publish_status_path(prefix, module_key, section_key))
    try:
        if not blob.exists():
            return None
        return json.loads(blob.download_as_text())
    except Exception as exc:
        logger.warning("[storage] Could not load publish_status for %s: %s", section_key, exc)
        return None


# ── Multi-reviewer validation package (demo "compliance officer / statistician / QA") ─
# Required gates — all three must approve before a section is release-ready.
GATE_ROLES_REQUIRED: tuple[str, ...] = ("statistician", "medical_writer", "qa_compliance")
# Full set of roles accepted by the API (the extras are optional approvals).
GATE_ROLES_ALL: tuple[str, ...] = GATE_ROLES_REQUIRED + (
    "clinical_lead", "regulatory", "pharmacovigilance",
)
ROLE_LABELS: dict[str, str] = {
    "statistician":      "Clinical Statistician",
    "medical_writer":    "Medical Writer",
    "qa_compliance":     "QA / Compliance Officer",
    "clinical_lead":     "Clinical Lead",
    "regulatory":        "Regulatory Affairs",
    "pharmacovigilance": "Pharmacovigilance",
}


def record_section_approval(
    bucket_name: str,
    program: ProgramInfo,
    module_key: str,
    section_key: str,
    approver: str,
    approval_reason: str,
    run_id: str,
    role: str = "qa_compliance",
) -> dict:
    """Role-scoped human approval — one step of the validation-package chain.

    Writes the approval into ``publish_status.gates[role]`` and, once every
    required role has signed, flips ``human_approved=True`` + ``release_ready=True``.

    Enforces:
      * publish_status.json exists (validator has run)
      * publish not validator-blocked
      * ``approver`` != document author (segregation of duties, case-insensitive)
      * same approver cannot sign two different gates on the same section
        (no one-person rubber-stamp)
      * each role can only be signed once

    Also writes an immutable witness record to
    ``approvals/{section_key}/{role}__{timestamp}__{approver}.json``.
    """
    prefix = program_prefix(program)
    bkt    = gcs().bucket(bucket_name)

    if role not in GATE_ROLES_ALL:
        raise ValueError(
            f"Unknown role '{role}'. Allowed: {', '.join(GATE_ROLES_ALL)}."
        )

    status = load_publish_status(bucket_name, program, module_key, section_key)
    if status is None:
        raise ValueError(
            f"No publish_status for {module_key}/{section_key} — "
            "an admin must call /documents/admin/seed-publish-status first."
        )

    author = (status.get("author") or "").strip().lower()
    approver_norm = (approver or "").strip().lower()
    if not approver_norm:
        raise ValueError("approver email is required.")
    if approver_norm == author:
        raise ValueError(
            f"Self-approval blocked: approver '{approver}' is the same as author."
        )
    if not status.get("publish_approved", False):
        raise ValueError(
            f"Section is publish-blocked by validator "
            f"(reason: {status.get('publish_blocked_reason', 'unknown')}). "
            "Re-run /write to fix errors before approving."
        )

    gates = dict(status.get("gates") or {})
    if role in gates:
        existing = gates[role].get("approver", "?")
        raise ValueError(
            f"Gate '{role}' already signed by {existing}. Each role signs once."
        )
    # No single person may sign two gates — multi-reviewer integrity.
    for prior_role, prior in gates.items():
        if (prior.get("approver") or "").strip().lower() == approver_norm:
            raise ValueError(
                f"Approver '{approver}' already signed gate '{prior_role}'. "
                "Each gate needs a different reviewer."
            )

    now_iso = datetime.now(timezone.utc).isoformat()
    gates[role] = {
        "approver":        approver,
        "approved_at":     now_iso,
        "approval_reason": approval_reason,
        "run_id":          run_id or status.get("run_id", ""),
    }

    release_ready = all(r in gates for r in GATE_ROLES_REQUIRED)
    status["gates"]          = gates
    status["release_ready"]  = release_ready
    status["required_roles"] = list(GATE_ROLES_REQUIRED)
    # Backward-compat surface — the last approval populates the legacy fields.
    status["human_approved"] = release_ready
    status["approver"]       = approver
    status["approved_at"]    = now_iso
    status["approval_reason"] = approval_reason

    bkt.blob(_publish_status_path(prefix, module_key, section_key)).upload_from_string(
        json.dumps(status, indent=2),
        content_type="application/json",
    )

    # Per-role immutable witness record
    approver_safe = approver.replace("@", "_at_").replace("/", "_")
    ts_safe       = now_iso.replace(":", "-")
    approval_blob_name = (
        f"{prefix}/approvals/{section_key}/{role}__{ts_safe}__{approver_safe}.json"
    )
    bkt.blob(approval_blob_name).upload_from_string(
        json.dumps({
            "run_id":          run_id or status.get("run_id", ""),
            "author":          status.get("author", ""),
            "approver":        approver,
            "role":            role,
            "role_label":      ROLE_LABELS.get(role, role),
            "approved_at":     now_iso,
            "module_key":      module_key,
            "section_key":     section_key,
            "gcs_path":        status.get("gcs_path", ""),
            "approval_reason": approval_reason,
            "validation_gcs_path": status.get("validation_gcs_path", ""),
            "release_ready":   release_ready,
        }, indent=2),
        content_type="application/json",
    )

    emit_section_approved(
        run_id=run_id or status.get("run_id", ""),
        author=status.get("author", "system"),
        approver=approver,
        therapeutic_area=program.therapeutic_area,
        disease_type=program.disease_type,
        drug_name=program.drug_name,
        module_key=module_key,
        section_key=section_key,
        gcs_path=status.get("gcs_path", ""),
        approval_reason=f"[{role}] {approval_reason}",
    )
    logger.info(
        "[storage] Gate '%s' signed %s/%s by %s (release_ready=%s, author=%s)",
        role, module_key, section_key, approver, release_ready,
        status.get("author", "system"),
    )
    return status


def record_section_rejection(
    bucket_name: str,
    program: ProgramInfo,
    module_key: str,
    section_key: str,
    reviewer: str,
    rejection_reason: str,
    run_id: str,
    role: str = "qa_compliance",
) -> dict:
    """Role-scoped rejection — clears all gates and blocks release.

    Writes an immutable rejection witness record and resets ``publish_status``
    so the author must revise and re-generate before any further approvals.

    Enforces:
      * publish_status.json exists
      * ``reviewer`` != document author (segregation of duties)
    """
    if role not in GATE_ROLES_ALL:
        raise ValueError(
            f"Unknown role '{role}'. Allowed: {', '.join(GATE_ROLES_ALL)}."
        )

    status = load_publish_status(bucket_name, program, module_key, section_key)
    if status is None:
        raise ValueError(
            f"No publish_status for {module_key}/{section_key} — "
            "an admin must call /documents/admin/seed-publish-status first."
        )

    author = (status.get("author") or "").strip().lower()
    reviewer_norm = (reviewer or "").strip().lower()
    if not reviewer_norm:
        raise ValueError("reviewer email is required.")
    if reviewer_norm == author:
        raise ValueError(
            f"Self-rejection blocked: reviewer '{reviewer}' is the same as author."
        )

    prefix = program_prefix(program)
    bkt    = gcs().bucket(bucket_name)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Clear all gates and mark rejected so no approvals can proceed until revised
    status["gates"]                 = {}
    status["release_ready"]         = False
    status["human_approved"]        = False
    status["rejected"]              = True
    status["rejected_by"]           = reviewer
    status["rejected_at"]           = now_iso
    status["rejected_role"]         = role
    status["rejection_reason"]      = rejection_reason
    status["publish_approved"]      = False
    status["publish_blocked_reason"] = (
        f"Rejected by {reviewer} ({ROLE_LABELS.get(role, role)}): {rejection_reason}"
    )

    bkt.blob(_publish_status_path(prefix, module_key, section_key)).upload_from_string(
        json.dumps(status, indent=2),
        content_type="application/json",
    )

    # Immutable rejection witness record
    reviewer_safe = reviewer.replace("@", "_at_").replace("/", "_")
    ts_safe       = now_iso.replace(":", "-")
    rejection_blob_name = (
        f"{prefix}/rejections/{section_key}/{role}__{ts_safe}__{reviewer_safe}.json"
    )
    bkt.blob(rejection_blob_name).upload_from_string(
        json.dumps({
            "run_id":           run_id or status.get("run_id", ""),
            "author":           status.get("author", ""),
            "reviewer":         reviewer,
            "role":             role,
            "role_label":       ROLE_LABELS.get(role, role),
            "rejected_at":      now_iso,
            "module_key":       module_key,
            "section_key":      section_key,
            "gcs_path":         status.get("gcs_path", ""),
            "rejection_reason": rejection_reason,
        }, indent=2),
        content_type="application/json",
    )

    logger.info(
        "[storage] Gate '%s' REJECTED %s/%s by %s (author=%s)",
        role, module_key, section_key, reviewer, status.get("author", "system"),
    )
    return status


def admin_seed_publish_status(
    bucket_name: str,
    program: ProgramInfo,
    module_key: str,
    section_key: str,
    admin_email: str,
    seed_reason: str,
) -> dict:
    """Admin-only action: create a publish_status.json for a section that was
    generated before the validation-pipeline existed (e.g. via content_worker
    without a /write validator pass).

    This explicitly names the admin who vouches for the section and the reason,
    creating a clear audit trail.  It does NOT auto-approve existing sections
    silently — the admin takes personal accountability in the audit log.

    Enforces:
      - No existing publish_status (sections that already went through /write
        must not be overridden this way).
      - admin_email and seed_reason are required (audit trail).
    """
    if not admin_email or not admin_email.strip():
        raise ValueError("admin_email is required.")
    if not seed_reason or len(seed_reason.strip()) < 5:
        raise ValueError("seed_reason is required (min 5 chars) for audit trail.")

    existing = load_publish_status(bucket_name, program, module_key, section_key)
    if existing is not None:
        raise ValueError(
            f"publish_status already exists for {module_key}/{section_key}. "
            "Use /write to re-run the validator instead of seeding."
        )

    prefix = program_prefix(program)
    bkt    = gcs().bucket(bucket_name)
    now_iso = datetime.now(timezone.utc).isoformat()
    gcs_path = f"{prefix}/ctd/{module_key}/{section_key}/document.md"

    status: dict = {
        "run_id":                 f"admin-seed-{now_iso}",
        "author":                 "system",
        "seeded_by_admin":        admin_email.strip(),
        "seed_reason":            seed_reason.strip(),
        "timestamp":              now_iso,
        "module_key":             module_key,
        "section_key":            section_key,
        "gcs_path":               gcs_path,
        "validation_gcs_path":    "",
        "validation_passed":      True,
        "section_issues":         [],
        "publish_approved":       True,
        "publish_blocked_reason": "",
        "human_approved":         False,
        "approver":               "",
        "approved_at":            "",
        "approval_reason":        "",
        "gates":                  {},
        "release_ready":          False,
        "required_roles":         list(GATE_ROLES_REQUIRED),
    }
    status_path = _publish_status_path(prefix, module_key, section_key)
    bkt.blob(status_path).upload_from_string(
        json.dumps(status, indent=2),
        content_type="application/json",
    )
    logger.info(
        "[storage] Admin '%s' seeded publish_status for %s/%s — reason: %s",
        admin_email, module_key, section_key, seed_reason,
    )
    return status


def load_section_history(
    bucket_name: str,
    program: ProgramInfo,
    module_key: str,
    section_key: str,
) -> list[dict]:
    """Return a chronological audit trail for a section.

    Reads every immutable witness record from:
      - ``{prefix}/rejections/{section_key}/``   → event_type = "rejection"
      - ``{prefix}/approvals/{section_key}/``    → event_type = "approval"
      - ``{prefix}/ctd/{module_key}/{section_key}/versions/manifest.json``
        → event_type = "generation"

    Each event dict has at minimum:
      event_type, timestamp, actor, role, reason, run_id
    Sorted oldest-first.
    """
    prefix = program_prefix(program)
    bkt    = gcs().bucket(bucket_name)
    events: list[dict] = []

    # ── rejections ────────────────────────────────────────────────────────────
    rej_prefix = f"{prefix}/rejections/{section_key}/"
    for blob in bkt.list_blobs(prefix=rej_prefix):
        try:
            data = json.loads(blob.download_as_text())
            events.append({
                "event_type": "rejection",
                "timestamp":  data.get("rejected_at", ""),
                "actor":      data.get("reviewer", ""),
                "role":       ROLE_LABELS.get(data.get("role", ""), data.get("role", "")),
                "reason":     data.get("rejection_reason", ""),
                "run_id":     data.get("run_id", ""),
                "author":     data.get("author", ""),
            })
        except Exception:
            pass

    # ── approvals ─────────────────────────────────────────────────────────────
    apv_prefix = f"{prefix}/approvals/{section_key}/"
    for blob in bkt.list_blobs(prefix=apv_prefix):
        try:
            data = json.loads(blob.download_as_text())
            events.append({
                "event_type": "approval",
                "timestamp":  data.get("approved_at", ""),
                "actor":      data.get("approver", ""),
                "role":       ROLE_LABELS.get(data.get("role", ""), data.get("role", "")),
                "reason":     data.get("approval_reason", ""),
                "run_id":     data.get("run_id", ""),
                "author":     data.get("author", ""),
                "release_ready": bool(data.get("release_ready", False)),
            })
        except Exception:
            pass

    # ── generation versions (manifest) ────────────────────────────────────────
    manifest_blob = bkt.blob(
        f"{prefix}/ctd/{module_key}/{section_key}/versions/manifest.json"
    )
    try:
        if manifest_blob.exists():
            for entry in json.loads(manifest_blob.download_as_text()):
                events.append({
                    "event_type": "generation",
                    "timestamp":  entry.get("timestamp", ""),
                    "actor":      entry.get("author", ""),
                    "role":       "Author",
                    "reason":     f"Version {entry.get('version', '?')} generated",
                    "run_id":     entry.get("run_id", ""),
                    "version":    entry.get("version", ""),
                })
    except Exception:
        pass

    events.sort(key=lambda e: e.get("timestamp", ""))
    return events


def load_gate_status(
    bucket_name: str,
    program: ProgramInfo,
    module_key: str,
    section_key: str,
) -> dict:
    """Rollup view of the validation-package gates for one section."""
    status = load_publish_status(bucket_name, program, module_key, section_key) or {}
    gates  = dict(status.get("gates") or {})
    return {
        "module_key":         module_key,
        "section_key":        section_key,
        "gcs_path":           status.get("gcs_path", ""),
        "current_version":    status.get("version"),
        "author":             status.get("author", ""),
        "publish_approved":   bool(status.get("publish_approved", False)),
        "publish_blocked_reason": status.get("publish_blocked_reason", ""),
        "required_roles":     list(GATE_ROLES_REQUIRED),
        "role_labels":        {r: ROLE_LABELS[r] for r in GATE_ROLES_ALL},
        "gates":              gates,
        "release_ready":      bool(status.get("release_ready", False)),
        "validation_gcs_path": status.get("validation_gcs_path", ""),
    }


# ── Template editing ──────────────────────────────────────────────────────────

def _template_path(prefix: str, module_key: str, section_key: str) -> str:
    return f"{prefix}/templates/{module_key}/{section_key}.md"


def _template_versions_prefix(prefix: str, module_key: str, section_key: str) -> str:
    return f"{prefix}/templates/{module_key}/versions/{section_key}"


def save_template(
    bucket_name: str,
    program: ProgramInfo,
    module_key: str,
    section_key: str,
    content: str,
    author: str,
    edit_reason: str = "",
) -> dict:
    """Save an edited template with per-template version history.

    Before overwriting, the current template (if any) is snapshotted to
    ``templates/{module}/versions/{section}/v{N}.md`` and recorded in a
    sibling ``manifest.json``. The new content is then written to the
    canonical ``templates/{module}/{section}.md`` path.
    """
    prefix   = program_prefix(program)
    gcs_path = _template_path(prefix, module_key, section_key)
    vprefix  = _template_versions_prefix(prefix, module_key, section_key)
    bkt      = gcs().bucket(bucket_name)
    now_iso  = datetime.now(timezone.utc).isoformat()
    version  = 1

    current_blob = bkt.blob(gcs_path)
    try:
        if current_blob.exists():
            manifest = _load_version_manifest(bkt, vprefix)
            version = len(manifest) + 1
            versioned_path = f"{vprefix}/v{version}.md"
            bkt.copy_blob(current_blob, bkt, versioned_path)
            manifest.append({
                "version":     version,
                "timestamp":   now_iso,
                "author":      author or "system",
                "edit_reason": edit_reason,
                "gcs_path":    versioned_path,
            })
            _save_version_manifest(bkt, vprefix, manifest)
            logger.info(
                "[storage] Template snapshotted v%d → gs://%s/%s (author=%s)",
                version, bucket_name, versioned_path, author or "system",
            )
    except Exception as exc:
        logger.warning("[storage] Template versioning failed (non-fatal): %s", exc)

    bkt.blob(gcs_path).upload_from_string(
        content,
        content_type="text/markdown; charset=utf-8",
    )
    logger.info(
        "[storage] Template saved to gs://%s/%s by %s",
        bucket_name, gcs_path, author or "system",
    )
    return {
        "gcs_path":    gcs_path,
        "version":     version,
        "author":      author or "system",
        "timestamp":   now_iso,
        "edit_reason": edit_reason,
    }


def save_document_edit(
    bucket_name: str,
    program: ProgramInfo,
    module_key: str,
    section_key: str,
    content: str,
    author: str,
    edit_reason: str = "",
    run_id: str = "",
) -> dict:
    """Save a user-edited CTD section via the existing versioning pipeline.

    Wraps :func:`save_document` so manual edits are snapshotted into
    ``versions/v{N}.md`` exactly like regenerations. ``module_label`` is
    derived from ``module_key`` (e.g. ``module2`` → ``Module 2``).
    """
    module_label = module_key.replace("module", "Module ").strip()
    section_label = section_key.replace("_", " ").title()
    doc = SectionDocument(
        module_key=module_key,
        module_label=module_label,
        section_key=section_key,
        section_label=section_label,
        content=content,
    )
    gcs_path = save_document(
        bucket_name=bucket_name,
        program=program,
        doc=doc,
        run_id=run_id or f"edit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
        author=author or "system",
    )
    return {
        "gcs_path":    gcs_path,
        "author":      author or "system",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "edit_reason": edit_reason,
    }
