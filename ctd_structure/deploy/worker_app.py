"""
ctd_structure/deploy/worker_app.py

Pub/Sub push-subscriber worker for CTD extraction.

Receives push messages from the `ctd-extraction` Pub/Sub topic,
runs the full LangGraph extraction pipeline, and persists the result
to GCS. Returns 200 to ack (done) or 5xx to nack (Pub/Sub retries).

Environment variables (injected by Cloud Run):
  ICH_INDEX_URL  — base URL of the ICH4/index service
  PORT           — port to bind (injected by Cloud Run)
  OPENAI_API_KEY — mounted from Secret Manager
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, status as http_status

_here      = os.path.dirname(os.path.abspath(__file__))
_pkg_root  = os.path.dirname(_here)
_workspace = os.path.dirname(_pkg_root)
for _p in [_workspace, _pkg_root]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ctd_structure.scaffold import scaffold_in_gcs          # noqa: E402
from ctd_structure.structure import extract_from_ich_index  # noqa: E402
from google.cloud import storage                            # noqa: E402

app = FastAPI(title="CTD Extraction Worker")

_gcs_client = storage.Client()

_GCS_TEMPLATE = "ctd_structure/ctd"


def _gcs_user_ctd_prefix(session_id: str) -> str:
    """Per-user draft CTD prefix — isolated from every other user's work."""
    return f"ctd_structure/users/{session_id}/ctd"


def _gcs_status_path(session_id: str) -> str:
    return f"ctd_structure/users/{session_id}/extraction_status.json"


def _gcs_session_path(session_id: str) -> str:
    return f"ctd_structure/users/{session_id}/session_state.json"


def _write_status(bucket_name: str, session_id: str, payload: dict) -> None:
    payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    _gcs_client.bucket(bucket_name).blob(_gcs_status_path(session_id)).upload_from_string(
        json.dumps(payload), content_type="application/json"
    )


@app.post("/extract")
async def extract(request: Request):
    """Pub/Sub push endpoint — called by the push subscription."""
    body = await request.json()

    # Unwrap Pub/Sub push envelope
    try:
        raw  = body["message"]["data"]
        data = base64.b64decode(raw).decode("utf-8")
        msg  = json.loads(data)
    except Exception as exc:
        # Bad message format — ack it (returning 4xx would cause infinite retries)
        print(f"[worker] Malformed Pub/Sub message: {exc}")
        return {"status": "ignored", "reason": str(exc)}

    bucket       = msg.get("bucket", "")
    ich_url      = msg.get("ich_url", os.environ.get("ICH_INDEX_URL", ""))
    reviewer     = msg.get("reviewer_email")
    session_id   = msg.get("session_id", "default")

    if not bucket:
        return {"status": "ignored", "reason": "no bucket in message"}

    run_id = msg.get("run_id")

    # ── Idempotency guard ────────────────────────────────────────────────────
    # Pub/Sub delivers at-least-once. Check whether this message is a duplicate
    # before starting (potentially expensive) extraction work.
    try:
        raw_status = _gcs_client.bucket(bucket).blob(
            _gcs_status_path(session_id)
        ).download_as_text()
        current = json.loads(raw_status)
    except Exception:
        current = {}

    if current.get("status") == "done":
        print(f"[worker] Skipping duplicate — extraction already done (session={session_id}, run_id={run_id})")
        return {"status": "skipped", "reason": "already_done"}

    if current.get("status") == "running" and run_id:
        stored_run_id = current.get("run_id")
        if stored_run_id and stored_run_id != run_id:
            # A different, more-recent extraction request is already in-flight.
            # Ack this stale message so it doesn't re-run.
            print(f"[worker] Skipping stale duplicate (run_id={run_id} superseded by {stored_run_id}, session={session_id})")
            return {"status": "skipped", "reason": "superseded"}
    # ─────────────────────────────────────────────────────────────────────────

    _write_status(bucket, session_id, {"status": "running", "run_id": run_id})
    print(f"[worker] Starting extraction (run_id={run_id}) → gs://{bucket}/{_gcs_user_ctd_prefix(session_id)}/")

    try:
        output, evaluation = extract_from_ich_index(ich_url, reviewer_email=reviewer)
        folder_paths = output.to_folder_paths()

        bkt_obj = _gcs_client.bucket(bucket)
        # Write to user's private draft — not the shared canonical template
        scaffold_in_gcs(bkt_obj, _gcs_user_ctd_prefix(session_id), folder_paths)

        # Persist session state so Gradio restores it on refresh
        bkt_obj.blob(_gcs_session_path(session_id)).upload_from_string(
            json.dumps({"approved": False, "ctd_output": output.model_dump()}),
            content_type="application/json",
        )

        n = len(folder_paths)
        try:
            _write_status(bucket, session_id, {"status": "done", "n_paths": n, "run_id": run_id})
        except Exception as status_exc:
            # Log the write failure but do NOT re-raise — the extraction itself
            # succeeded and we must return 200 to ack the Pub/Sub message.
            print(f"[worker] WARNING: failed to write 'done' status: {status_exc}")
        print(f"[worker] Extraction complete — {n} paths written (session={session_id}).")
        return {"status": "ok", "n_paths": n}

    except Exception as exc:
        _write_status(bucket, session_id, {"status": "failed", "error": str(exc)})
        print(f"[worker] Extraction failed: {exc}")
        # 5xx → Pub/Sub will retry according to the subscription retry policy
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
