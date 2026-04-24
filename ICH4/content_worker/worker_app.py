"""
ICH4 Content Worker — Pub/Sub push-subscriber.

Receives a content-generation request and coordinates:
  1. Orchestrator POST /generate  →  templates (JSON)
  2. Saves templates to GCS       →  writer can find them
  3. Writer POST /write           →  fills placeholders, saves documents to GCS
  4. Writes final status to GCS   →  Gradio UI polls this

GCS status path:
  therapeutic-area/{ta}/{dis}/{drug}/content_status/{session_id}.json

Pub/Sub message envelope (base64-encoded JSON):
  {
    "bucket":    "my-bucket",
    "session_id": "abc123",
    "run_id":    "uuid",
    "program": {
      "therapeutic_area": "oncology",
      "disease_type":     "lung_cancer",
      "drug_name":        "carboplatin"
    }
  }

Environment variables (injected by Cloud Run):
  ORCHESTRATOR_URL  — base URL of the orchestrator service
  WRITER_URL        — base URL of the writer service
  PORT              — port to bind (injected by Cloud Run)
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from google.cloud import storage
from pydantic import BaseModel

try:
    from bq_audit import emit_generation_run, emit_generation_start, emit_validation_issue  # noqa: F401
except ImportError:
    def emit_generation_run(**_): pass    # noqa: E704
    def emit_generation_start(**_): pass  # noqa: E704
    def emit_validation_issue(**_): pass  # noqa: E704

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

app = FastAPI(title="ICH4 Content Worker", version="1.0.0")

_CONTENT_PIPELINE_URL  = os.environ.get("CONTENT_PIPELINE_URL", "").rstrip("/")
_WRITER_URL            = os.environ.get("WRITER_URL", "").rstrip("/")
_INDEX_URL             = os.environ.get("INDEX_URL", "").rstrip("/")
_CLINICAL_ANALYST_URL  = os.environ.get("CLINICAL_ANALYST_URL", "").rstrip("/")
_TIMEOUT               = float(os.environ.get("SERVICE_TIMEOUT", "600"))

_gcs_client = storage.Client()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _program_prefix(ta: str, dis: str, drug: str) -> str:
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_")
    return f"therapeutic-area/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}"


def _program_namespace(ta: str, dis: str, drug: str) -> str:
    """Build the base program namespace used for per-program LlamaIndex namespaces."""
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_")
    return f"program_{_slug(ta)}_{_slug(dis)}_{_slug(drug)}"


def _content_status_path(ta: str, dis: str, drug: str, session_id: str = "") -> str:
    suffix = session_id.strip() if session_id and session_id.strip() else "latest"
    return f"{_program_prefix(ta, dis, drug)}/content_status/{suffix}.json"


def _latest_status_path(ta: str, dis: str, drug: str) -> str:
    return f"{_program_prefix(ta, dis, drug)}/content_status/latest.json"


def _write_status(bucket: str, path: str, payload: dict, also_latest: str | None = None) -> None:
    stamped = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    data = json.dumps(stamped)
    # Derive latest.json path automatically (content_status/{uuid}.json → content_status/latest.json)
    if also_latest is None and "/content_status/" in path and not path.endswith("/latest.json"):
        also_latest = path.rsplit("/", 1)[0] + "/latest.json"
    try:
        _gcs_client.bucket(bucket).blob(path).upload_from_string(
            data, content_type="application/json"
        )
        # Always mirror to latest.json so the UI gets the most recent status
        if also_latest and also_latest != path:
            _gcs_client.bucket(bucket).blob(also_latest).upload_from_string(
                data, content_type="application/json"
            )
    except Exception as exc:
        logger.warning("[worker] Could not write status to GCS: %s", exc)


def _oidc_headers(audience: str) -> dict[str, str]:
    """Return Authorization header with an OIDC token for Cloud Run service calls."""
    try:
        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2.id_token import fetch_id_token
        token = fetch_id_token(AuthRequest(), audience)
        return {"Authorization": f"Bearer {token}"}
    except Exception as exc:
        logger.warning("[worker] Could not obtain OIDC token for %s: %s", audience, exc)
        return {}


def _service_audience(base_url: str) -> str:
    """Strip path from URL to use as OIDC audience (Cloud Run requires the base URL)."""
    from urllib.parse import urlparse
    p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Schema-evolution patch trigger ────────────────────────────────────────────

class SchemaPatchRequest(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str
    bucket: str
    run_validator: bool = False
    author: str = ""
    run_id: str = ""


@app.post("/schema-patch")
async def schema_patch(request: SchemaPatchRequest):
    """Check for clinical data schema changes and patch affected CTD sections.

    Orchestrates the schema-evolution validator loop:
      1. Calls clinical-analyst POST /schema-diff  — compares manifest vs snapshot.
      2. If changes are found, calls writer POST /patch for affected sections only.
      3. Returns a summary of what changed and what was patched.

    The UI or an automated post-upload hook calls this endpoint after every
    new clinical CSV upload to keep documents in sync with the evolving schema.
    """
    ta   = request.therapeutic_area
    dis  = request.disease_type
    drug = request.drug_name
    bucket = request.bucket
    author = request.author
    run_id = request.run_id

    if not _CLINICAL_ANALYST_URL:
        return {"status": "skipped", "reason": "CLINICAL_ANALYST_URL not configured"}
    if not _WRITER_URL:
        return {"status": "skipped", "reason": "WRITER_URL not configured"}

    # ── Step 1: Detect schema changes ─────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            analyst_aud = _service_audience(_CLINICAL_ANALYST_URL)
            r = await client.post(
                f"{_CLINICAL_ANALYST_URL}/schema-diff",
                json={
                    "therapeutic_area": ta,
                    "disease_type":     dis,
                    "drug_name":        drug,
                    "bucket":           bucket,
                },
                headers=_oidc_headers(analyst_aud),
            )
            if r.status_code == 404:
                return {"status": "no_manifest", "reason": "No clinical manifest found."}
            if r.status_code >= 400:
                return {
                    "status": "error",
                    "reason": f"schema-diff HTTP {r.status_code}: {r.text[:200]}",
                }
            diff = r.json()
    except Exception as exc:
        logger.error("[schema-patch] schema-diff failed: %s", exc)
        return {"status": "error", "reason": str(exc)}

    if not diff.get("has_changes"):
        return {
            "status":  "no_changes",
            "message": "Clinical data schema unchanged — no patch needed.",
        }

    affected_sections  = diff.get("affected_sections", [])
    added_placeholders = diff.get("added_placeholders", [])
    changed_placeholders = diff.get("changed_placeholders", [])
    removed_placeholders = diff.get("removed_placeholders", [])
    changed_keys = sorted(set(added_placeholders + changed_placeholders + removed_placeholders))

    logger.info(
        "[schema-patch] Schema changed — added=%s changed=%s removed=%s  affected sections=%s",
        added_placeholders, changed_placeholders, removed_placeholders, affected_sections,
    )

    # ── Step 1.5: Pre-resolve fresh values for changed keys ───────────────────
    # clinical-analyst owns all data computation; writer receives values, never
    # computes them.  Resolve only the changed keys so the writer has up-to-date
    # numbers before patching prose.
    resolved_values: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            analyst_aud = _service_audience(_CLINICAL_ANALYST_URL)
            rr = await client.post(
                f"{_CLINICAL_ANALYST_URL}/resolve",
                json={
                    "therapeutic_area": ta,
                    "disease_type":     dis,
                    "drug_name":        drug,
                    "bucket":           bucket,
                    "placeholder_keys": changed_keys,
                    "author":           author,
                    "run_id":           run_id,
                },
                headers=_oidc_headers(analyst_aud),
            )
            if rr.status_code == 200:
                resolved_values = rr.json().get("resolved_values", {})
                logger.info(
                    "[schema-patch] Resolved %d/%d changed keys via /resolve",
                    len(resolved_values), len(changed_keys),
                )
            else:
                logger.warning(
                    "[schema-patch] /resolve HTTP %s — writer will use [DATA PENDING] for unresolved keys",
                    rr.status_code,
                )
    except Exception as exc:
        logger.warning("[schema-patch] /resolve failed (non-fatal): %s", exc)

    # ── Step 2: Patch affected sections ───────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            writer_aud = _service_audience(_WRITER_URL)
            rw = await client.post(
                f"{_WRITER_URL}/patch",
                json={
                    "program": {
                        "therapeutic_area": ta,
                        "disease_type":     dis,
                        "drug_name":        drug,
                    },
                    "bucket_name":     bucket,
                    "sections":        affected_sections,
                    "changed_keys":    changed_keys,
                    "resolved_values": resolved_values,
                    "run_validator":   request.run_validator,
                },
                headers=_oidc_headers(writer_aud),
            )
            if rw.status_code >= 400:
                return {
                    "status": "error",
                    "reason": f"writer /patch HTTP {rw.status_code}: {rw.text[:200]}",
                }
            patch_result = rw.json()
    except Exception as exc:
        logger.error("[schema-patch] writer /patch failed: %s", exc)
        return {"status": "error", "reason": str(exc)}

    return {
        "status":               "patched",
        "added_placeholders":   added_placeholders,
        "changed_placeholders": changed_placeholders,
        "removed_placeholders": removed_placeholders,
        "affected_sections":    affected_sections,
        "sections_written":     patch_result.get("sections_written", 0),
        "sections_failed":      patch_result.get("sections_failed", []),
        "validation_passed":    patch_result.get("validation", {}).get("passed"),
    }


@app.post("/generate")
async def generate(request: Request):
    """Pub/Sub push endpoint — called by the Pub/Sub push subscription."""
    body = await request.json()

    # ── Unwrap Pub/Sub push envelope ─────────────────────────────────────────
    try:
        raw  = body["message"]["data"]
        data = base64.b64decode(raw).decode("utf-8")
        msg  = json.loads(data)
    except Exception as exc:
        # Malformed message — ack it (4xx would cause infinite retries)
        logger.warning("[worker] Malformed Pub/Sub message: %s", exc)
        return {"status": "ignored", "reason": str(exc)}

    bucket     = msg.get("bucket", "")
    session_id = msg.get("session_id", "default")
    run_id     = msg.get("run_id", "")
    author     = msg.get("author", "")   # IAP user email, set by UI on trigger
    program    = msg.get("program", {})

    ta   = program.get("therapeutic_area", "")
    dis  = program.get("disease_type", "")
    drug = program.get("drug_name", "")

    if not (bucket and ta and dis and drug):
        logger.warning("[worker] Missing required fields — ignoring message")
        return {"status": "ignored", "reason": "missing required fields"}

    status_path = _content_status_path(ta, dis, drug, session_id)

    # ── Idempotency: skip if this run_id was already completed ───────────────
    try:
        existing = json.loads(
            _gcs_client.bucket(bucket).blob(status_path).download_as_text()
        )
        if existing.get("status") == "done" and existing.get("run_id") == run_id:
            logger.info("[worker] Skipping duplicate run_id=%s", run_id)
            return {"status": "duplicate"}
    except Exception:
        pass  # first run — status file doesn't exist yet

    _write_status(bucket, status_path, {
        "status": "running", "run_id": run_id, "step": "starting",
    })
    emit_generation_start(
        run_id=run_id,
        author=author or "system",
        therapeutic_area=ta,
        disease_type=dis,
        drug_name=drug,
        session_id=session_id,
    )
    logger.info("[worker] Content generation started  %s/%s/%s  session=%s", ta, dis, drug, session_id)

    # ── ICH M4E(R2) evidence-ordered generation ───────────────────────────────
    # Module 5 CSRs are the primary evidence layer (ICH M4E(R2) §5.3).
    # Module 2.7 Clinical Summary aggregates Module 5 findings (ICH M4E(R2) §2.7).
    # Module 2.5 Clinical Overview critically analyses Module 2.7+5 (ICH M4E(R2) §2.5).
    # Each pass indexes its output into a per-program LlamaIndex namespace so
    # downstream passes can retrieve actual generated content via RAG, preventing
    # hallucinated statistics in Module 2 sections.
    GENERATION_DAG = [
        {
            "pass_id":          "module5",
            "pass_label":       "Module 5: Clinical Study Reports",
            "module_filter":    ["module5"],
            "section_prefixes": [],
            "evidence_from":    [],
            "index_after":      True,
        },
        {
            "pass_id":          "module2_clinical_summary",
            "pass_label":       "Module 2.7: Clinical Summary",
            "module_filter":    ["module2"],
            "section_prefixes": ["2.7"],
            "evidence_from":    ["module5"],
            "index_after":      True,
        },
        {
            "pass_id":          "module2_overview",
            "pass_label":       "Module 2.5/2.4/2.3: Overviews",
            "module_filter":    ["module2"],
            "section_prefixes": ["2.5", "2.4", "2.3", "2.2", "2.1"],
            "evidence_from":    ["module5", "module2_clinical_summary"],
            "index_after":      False,
        },
    ]

    base_ns      = _program_namespace(ta, dis, drug)
    total_passes = len(GENERATION_DAG)
    all_sections_written = 0
    all_sections_failed: list[str] = []
    final_validation: dict = {}

    try:
        for pass_idx, dag_pass in enumerate(GENERATION_DAG):
            pass_id    = dag_pass["pass_id"]
            pass_label = dag_pass["pass_label"]
            evidence_namespaces = [f"{base_ns}_{eid}" for eid in dag_pass["evidence_from"]]

            _write_status(bucket, status_path, {
                "status":       "running",
                "run_id":       run_id,
                "step":         f"pass_{pass_id}",
                "pass_label":   pass_label,
                "pass_number":  pass_idx + 1,
                "total_passes": total_passes,
            })
            logger.info("[worker] Pass %d/%d: %s", pass_idx + 1, total_passes, pass_label)

            # ── Step A: Orchestrator → templates ──────────────────────────────
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                orch_aud = _service_audience(_CONTENT_PIPELINE_URL)
                r = await client.post(
                    f"{_CONTENT_PIPELINE_URL}/generate",
                    json={
                        "program": {
                            "therapeutic_area": ta,
                            "disease_type":     dis,
                            "drug_name":        drug,
                        },
                        "bucket":                bucket,
                        "include_clinical_data": True,
                        "include_ich_context":   True,
                        "module_filter":         dag_pass["module_filter"],
                        "evidence_namespaces":   evidence_namespaces,
                        "section_key_prefixes":  dag_pass["section_prefixes"],
                    },
                    headers=_oidc_headers(orch_aud),
                )
                if r.status_code >= 400:
                    detail = (
                        f"orchestrator (pass={pass_id}) returned HTTP "
                        f"{r.status_code}: {r.text[:300]}"
                    )
                    _write_status(bucket, status_path, {
                        "status": "failed", "run_id": run_id, "error": detail,
                    })
                    if r.status_code < 500:
                        return {"status": "failed", "reason": detail}
                    raise httpx.HTTPStatusError(detail, request=r.request, response=r)

                orch_data  = r.json()
                templates  = orch_data.get("templates", [])
                resolved_values: dict[str, str] = orch_data.get("resolved_values", {})
                logger.info(
                    "[worker] Pass %s: orchestrator returned %d templates, %d resolved values",
                    pass_id, len(templates), len(resolved_values),
                )

            if not templates:
                detail = (
                    f"Pass {pass_id}: content pipeline returned no templates for "
                    f"{ta}/{dis}/{drug}. "
                    "Ensure the ICH CTD structure is approved and the program folder is scaffolded "
                    "before triggering content generation."
                )
                logger.error("[worker] %s", detail)
                _write_status(bucket, status_path, {
                    "status": "failed",
                    "run_id": run_id,
                    "error":  detail,
                })
                return {"status": "failed", "reason": detail}

            # ── Step B: Save templates to GCS ────────────────────────────────
            prefix = _program_prefix(ta, dis, drug)
            bkt    = _gcs_client.bucket(bucket)
            for tmpl in templates:
                gcs_path = (
                    f"{prefix}/templates"
                    f"/{tmpl['module_key']}"
                    f"/{tmpl['section_key']}.md"
                )
                bkt.blob(gcs_path).upload_from_string(
                    tmpl["content"],
                    content_type="text/markdown; charset=utf-8",
                )
            logger.info(
                "[worker] Pass %s: saved %d templates to gs://%s/%s/templates/",
                pass_id, len(templates), bucket, prefix,
            )

            # ── Step C: Writer ────────────────────────────────────────────────
            _write_status(bucket, status_path, {
                "status":     "running",
                "run_id":     run_id,
                "step":       f"writing_{pass_id}",
                "pass_label": pass_label,
            })
            section_keys = [t["section_key"] for t in templates]
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                writer_aud = _service_audience(_WRITER_URL)
                r = await client.post(
                    f"{_WRITER_URL}/write",
                    json={
                        "program": {
                            "therapeutic_area": ta,
                            "disease_type":     dis,
                            "drug_name":        drug,
                        },
                        "bucket_name":     bucket,
                        "sections":        section_keys,
                        "run_validator":   pass_idx == total_passes - 1,
                        "resolved_values": resolved_values,
                        "run_id":          run_id,
                        "author":          author,
                    },
                    headers=_oidc_headers(writer_aud),
                )
                if r.status_code >= 400:
                    detail = (
                        f"writer (pass={pass_id}) returned HTTP "
                        f"{r.status_code}: {r.text[:300]}"
                    )
                    _write_status(bucket, status_path, {
                        "status": "failed", "run_id": run_id, "error": detail,
                    })
                    if r.status_code < 500:
                        return {"status": "failed", "reason": detail}
                    raise httpx.HTTPStatusError(detail, request=r.request, response=r)

                writer_data      = r.json()
                sections_written = writer_data.get("sections_written", 0)
                sections_failed  = writer_data.get("sections_failed", [])
                final_validation = writer_data.get("validation", {})
                all_sections_written += sections_written
                all_sections_failed.extend(sections_failed)
                logger.info(
                    "[worker] Pass %s: written=%d  failed=%s",
                    pass_id, sections_written, sections_failed,
                )

            # ── Step D: Ingest into program index (if configured) ─────────────
            if dag_pass.get("index_after") and _INDEX_URL and writer_data.get("documents"):
                pass_namespace  = f"{base_ns}_{pass_id}"
                ingest_sections = [
                    {
                        "module_key":  doc["module_key"],
                        "section_key": doc["section_key"],
                        "content":     doc["content"],
                    }
                    for doc in writer_data["documents"]
                    if doc.get("content", "").strip()
                ]
                if ingest_sections:
                    try:
                        async with httpx.AsyncClient(timeout=120.0) as client:
                            idx_aud = _service_audience(_INDEX_URL)
                            ir = await client.post(
                                f"{_INDEX_URL}/index/ingest-program",
                                json={
                                    "program_namespace": pass_namespace,
                                    "sections":          ingest_sections,
                                },
                                headers=_oidc_headers(idx_aud),
                            )
                            if ir.status_code == 200:
                                logger.info(
                                    "[worker] Pass %s: indexed %d sections -> %s",
                                    pass_id, len(ingest_sections), pass_namespace,
                                )
                            else:
                                logger.warning(
                                    "[worker] Pass %s: index ingest HTTP %s — continuing",
                                    pass_id, ir.status_code,
                                )
                    except Exception as exc:
                        logger.warning(
                            "[worker] Pass %s: index ingest failed (non-fatal): %s",
                            pass_id, exc,
                        )

    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        # Transient infra error — nack (Pub/Sub will retry)
        _write_status(bucket, status_path, {
            "status": "failed", "run_id": run_id, "error": f"transient: {exc}",
        })
        raise  # 5xx → Pub/Sub retries

    except Exception as exc:
        if not isinstance(exc, httpx.HTTPStatusError):
            _write_status(bucket, status_path, {
                "status": "failed", "run_id": run_id, "error": str(exc),
            })
            logger.error("[worker] Unexpected error: %s", exc, exc_info=True)
            return {"status": "failed", "reason": str(exc)}
        raise

    # ── Save full validation report to GCS (non-fatal) ───────────────────────
    prefix = _program_prefix(ta, dis, drug)
    validation_report_path = f"{prefix}/content_status/{session_id}_validation.json"
    validation_latest_path = f"{prefix}/content_status/validation_report.json"
    try:
        report_payload = json.dumps({
            "run_id":          run_id,
            "author":          author or "system",
            "generated_at":    datetime.now(timezone.utc).isoformat(),
            "passed":          final_validation.get("passed", False),
            "summary":         final_validation.get("summary", ""),
            "sections_written": all_sections_written,
            "sections_failed": all_sections_failed,
            "issues":          final_validation.get("issues", []),
        }, indent=2)
        _gcs_client.bucket(bucket).blob(validation_report_path).upload_from_string(
            report_payload, content_type="application/json"
        )
        _gcs_client.bucket(bucket).blob(validation_latest_path).upload_from_string(
            report_payload, content_type="application/json"
        )
        logger.info("[worker] Saved validation report to gs://%s/%s", bucket, validation_latest_path)
    except Exception as exc:
        logger.warning("[worker] Validation report save failed (non-fatal): %s", exc)

    # ── Emit audit events ─────────────────────────────────────────────────────
    issues = final_validation.get("issues", [])
    emit_generation_run(
        run_id=run_id,
        author=author or "system",
        therapeutic_area=ta,
        disease_type=dis,
        drug_name=drug,
        sections_written=all_sections_written,
        sections_failed=all_sections_failed,
        validation_passed=bool(final_validation.get("passed", False)),
        validation_issue_count=len(issues),
    )
    for issue in issues:
        emit_validation_issue(
            run_id=run_id,
            author=author or "system",
            therapeutic_area=ta,
            disease_type=dis,
            drug_name=drug,
            section_key=issue.get("section_key", ""),
            severity=issue.get("severity", ""),
            issue_message=issue.get("message", ""),
        )

    # ── Write final done status ───────────────────────────────────────────────
    _write_status(bucket, status_path, {
        "status":             "done",
        "run_id":             run_id,
        "sections_written":   all_sections_written,
        "sections_failed":    all_sections_failed,
        "validation_passed":  final_validation.get("passed", False),
        "validation_summary": final_validation.get("summary", ""),
        "validation_report_path": f"gs://{bucket}/{validation_latest_path}",
    })
    logger.info(
        "[worker] Generation complete  written=%d  validation_passed=%s",
        all_sections_written, final_validation.get("passed"),
    )
    return {"status": "ok", "sections_written": all_sections_written}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
