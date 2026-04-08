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

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

app = FastAPI(title="ICH4 Content Worker", version="1.0.0")

_ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")
_WRITER_URL       = os.environ.get("WRITER_URL", "").rstrip("/")
_TIMEOUT          = float(os.environ.get("SERVICE_TIMEOUT", "600"))

_gcs_client = storage.Client()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _program_prefix(ta: str, dis: str, drug: str) -> str:
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_")
    return f"therapeutic-area/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}"


def _content_status_path(ta: str, dis: str, drug: str, session_id: str) -> str:
    return f"{_program_prefix(ta, dis, drug)}/content_status/{session_id}.json"


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
        "status": "running", "run_id": run_id, "step": "orchestrator",
    })
    logger.info("[worker] Content generation started  %s/%s/%s  session=%s", ta, dis, drug, session_id)

    # ── Step 1: orchestrator → templates ─────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            orch_audience = _service_audience(_ORCHESTRATOR_URL)
            r = await client.post(
                f"{_ORCHESTRATOR_URL}/generate",
                json={
                    "program": {
                        "therapeutic_area": ta,
                        "disease_type":     dis,
                        "drug_name":        drug,
                    },
                    "include_clinical_data": True,
                    "include_ich_context":  True,
                },
                headers=_oidc_headers(orch_audience),
            )
            if r.status_code >= 400:
                detail = f"orchestrator returned HTTP {r.status_code}: {r.text[:300]}"
                _write_status(bucket, status_path, {
                    "status": "failed", "run_id": run_id, "error": detail,
                })
                # 4xx from orchestrator = permanent failure; ack to avoid infinite retry
                if r.status_code < 500:
                    return {"status": "failed", "reason": detail}
                raise httpx.HTTPStatusError(detail, request=r.request, response=r)

            templates = r.json().get("templates", [])
            logger.info("[worker] Orchestrator returned %d templates", len(templates))

        # ── Step 2: save templates to GCS ────────────────────────────────────
        _write_status(bucket, status_path, {
            "status": "running", "run_id": run_id,
            "step": "saving_templates", "templates_total": len(templates),
        })
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
        logger.info("[worker] Saved %d templates to gs://%s/%s/templates/", len(templates), bucket, prefix)

        # ── Step 3: writer /write ─────────────────────────────────────────────
        _write_status(bucket, status_path, {
            "status": "running", "run_id": run_id,
            "step": "writing", "templates_total": len(templates),
        })
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            writer_audience = _service_audience(_WRITER_URL)
            r = await client.post(
                f"{_WRITER_URL}/write",
                json={
                    "program": {
                        "therapeutic_area": ta,
                        "disease_type":     dis,
                        "drug_name":        drug,
                    },
                    "bucket_name":   bucket,
                    "run_validator": True,
                },
                headers=_oidc_headers(writer_audience),
            )
            if r.status_code >= 400:
                detail = f"writer returned HTTP {r.status_code}: {r.text[:300]}"
                _write_status(bucket, status_path, {
                    "status": "failed", "run_id": run_id, "error": detail,
                })
                if r.status_code < 500:
                    return {"status": "failed", "reason": detail}
                raise httpx.HTTPStatusError(detail, request=r.request, response=r)

            writer_data      = r.json()
            sections_written = writer_data.get("sections_written", 0)
            sections_failed  = writer_data.get("sections_failed", [])
            validation       = writer_data.get("validation", {})
            logger.info(
                "[worker] Writer done  written=%d  failed=%s  validation_passed=%s",
                sections_written, sections_failed, validation.get("passed"),
            )

    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        # Transient infra error — nack (Pub/Sub will retry)
        _write_status(bucket, status_path, {
            "status": "failed", "run_id": run_id, "error": f"transient: {exc}",
        })
        raise  # 5xx → Pub/Sub retries

    except Exception as exc:
        if not isinstance(exc, httpx.HTTPStatusError):
            # Unexpected / permanent error — ack to avoid infinite retry
            _write_status(bucket, status_path, {
                "status": "failed", "run_id": run_id, "error": str(exc),
            })
            logger.error("[worker] Unexpected error: %s", exc, exc_info=True)
            return {"status": "failed", "reason": str(exc)}
        raise

    # ── Write final done status ───────────────────────────────────────────────
    _write_status(bucket, status_path, {
        "status":              "done",
        "run_id":              run_id,
        "sections_written":    sections_written,
        "sections_failed":     sections_failed,
        "validation_passed":   validation.get("passed", False),
        "validation_summary":  validation.get("summary", ""),
    })
    logger.info(
        "[worker] Content generation complete  written=%d  validation_passed=%s",
        sections_written, validation.get("passed"),
    )
    return {"status": "ok", "sections_written": sections_written}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
