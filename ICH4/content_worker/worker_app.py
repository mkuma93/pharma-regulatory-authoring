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
import re
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from google.cloud import storage

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

app = FastAPI(title="ICH4 Content Worker", version="1.0.0")

_ORCHESTRATOR_URL      = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")
_WRITER_URL            = os.environ.get("WRITER_URL", "").rstrip("/")
_INDEX_URL             = os.environ.get("INDEX_URL", "").rstrip("/")
_CLINICAL_ANALYST_URL  = os.environ.get("CLINICAL_ANALYST_URL", "").rstrip("/")
_TIMEOUT               = float(os.environ.get("SERVICE_TIMEOUT", "600"))

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _extract_placeholder_keys(templates: list[dict]) -> list[str]:
    """Return unique {{placeholder}} keys found across all template contents."""
    keys: set[str] = set()
    for t in templates:
        keys.update(_PLACEHOLDER_RE.findall(t.get("content", "")))
    return sorted(keys)

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
    return f"{_program_prefix(ta, dis, drug)}/content_status/latest.json"


def _write_status(bucket: str, path: str, payload: dict) -> None:
    stamped = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        _gcs_client.bucket(bucket).blob(path).upload_from_string(
            json.dumps(stamped), content_type="application/json"
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
                orch_aud = _service_audience(_ORCHESTRATOR_URL)
                r = await client.post(
                    f"{_ORCHESTRATOR_URL}/generate",
                    json={
                        "program": {
                            "therapeutic_area": ta,
                            "disease_type":     dis,
                            "drug_name":        drug,
                        },
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

                templates = r.json().get("templates", [])
                logger.info(
                    "[worker] Pass %s: orchestrator returned %d templates",
                    pass_id, len(templates),
                )

            if not templates:
                logger.warning("[worker] Pass %s: no templates — skipping write", pass_id)
                continue

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

            # ── Step B.5: Clinical Analyst → pre-resolve placeholders ──────────
            resolved_values: dict[str, str] = {}
            if _CLINICAL_ANALYST_URL:
                placeholder_keys = _extract_placeholder_keys(templates)
                if placeholder_keys:
                    _write_status(bucket, status_path, {
                        "status":     "running",
                        "run_id":     run_id,
                        "step":       f"resolving_{pass_id}",
                        "pass_label": pass_label,
                    })
                    try:
                        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                            analyst_aud = _service_audience(_CLINICAL_ANALYST_URL)
                            ra = await client.post(
                                f"{_CLINICAL_ANALYST_URL}/resolve",
                                json={
                                    "therapeutic_area": ta,
                                    "disease_type":     dis,
                                    "drug_name":        drug,
                                    "bucket":           bucket,
                                    "placeholder_keys": placeholder_keys,
                                },
                                headers=_oidc_headers(analyst_aud),
                            )
                            if ra.status_code == 200:
                                resolved_values = ra.json().get("resolved_values", {})
                                logger.info(
                                    "[worker] Pass %s: analyst resolved %d/%d keys",
                                    pass_id, len(resolved_values), len(placeholder_keys),
                                )
                            elif ra.status_code == 404:
                                # No manifest yet — writer hybrid-analyst will handle it
                                logger.info(
                                    "[worker] Pass %s: no clinical manifest — skipping resolve",
                                    pass_id,
                                )
                            else:
                                logger.warning(
                                    "[worker] Pass %s: analyst /resolve HTTP %s — continuing",
                                    pass_id, ra.status_code,
                                )
                    except Exception as exc:
                        logger.warning(
                            "[worker] Pass %s: analyst resolve failed (non-fatal): %s",
                            pass_id, exc,
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

    # ── Write final done status ───────────────────────────────────────────────
    _write_status(bucket, status_path, {
        "status":             "done",
        "run_id":             run_id,
        "sections_written":   all_sections_written,
        "sections_failed":    all_sections_failed,
        "validation_passed":  final_validation.get("passed", False),
        "validation_summary": final_validation.get("summary", ""),
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
