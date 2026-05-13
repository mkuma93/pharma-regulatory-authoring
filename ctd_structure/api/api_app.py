"""
ctd_structure/api/api_app.py

FastAPI backend for the CTD Structure workflow.

Owns all GCS operations, Pub/Sub publishing, and CTD structure business logic.
The coordinator (intent classification) lives in ui/app.py and calls these
action-specific endpoints directly — no LLM runs here.

Endpoints
─────────
  POST /extract          → publish extraction job to Pub/Sub
  POST /approve          → scaffold canonical template in GCS
  POST /disapprove       → set awaiting_feedback state
  POST /refine           → re-query ICH index for feedback-targeted repair
  POST /copy             → scaffold program folder in GCS
  GET  /status_query     → extraction + content status as a reply string
  GET  /status           → raw extraction + content status dicts (for auto-poll)
  GET  /session          → load session state + folder paths
  GET  /health           → liveness probe

  Content generation triggering and clinical CSV upload live in clinical-analyst.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ── Path bootstrap ────────────────────────────────────────────────────────────
_here     = os.path.dirname(os.path.abspath(__file__))
_pkg_root = os.path.dirname(_here)          # ctd_structure/..  (repo root)
for _p in [_pkg_root, os.path.join(_pkg_root, "ctd_structure", "deploy")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from google.cloud import pubsub_v1, storage

from ctd_structure.scaffold import scaffold_in_gcs
from ctd_structure.structure import CTDStructureOutput, EvaluationResult, refine_from_feedback
from main import CoordinatorDecision  # Pydantic model only — no LLM calls in the API

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_URL    = os.environ.get("ICH_INDEX_URL",
                                 "https://ich4-index-your-service-id-uc.a.run.app")
_DEFAULT_BUCKET = os.environ.get("GCS_BUCKET",
                                 "your-gcs-bucket-name")
_GCS_TEMPLATE   = "ctd_structure/ctd"
_GCS_PROGRAMS   = "therapeutic-area"
_PUBSUB_TOPIC         = os.environ.get("PUBSUB_TOPIC", "ctd-extraction")
_GCP_PROJECT    = os.environ.get("GCP_PROJECT_ID", "your-gcp-project-id")
_RUNNING_TIMEOUT_SECONDS        = 20 * 60   # 20 min
# Content status timeout is used only by the combined GET /status auto-poll below.
# ctd-api does not generate content — it just reads the status blob that
# content_worker writes, so the UI can get extraction + content state in one call.
_CONTENT_STATUS_TIMEOUT_SECONDS = 30 * 60   # 30 min

# ── GCS client (module-level singleton) ───────────────────────────────────────

_gcs_client = storage.Client()

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="CTD Structure API", version="1.0.0")


# ═══════════════════════════════════════════════════════════════════════════════
# Request / response models
# ═══════════════════════════════════════════════════════════════════════════════

class ActionResponse(BaseModel):
    """Standard response for all action endpoints."""
    reply: str
    state_patch: dict = {}


class ExtractRequest(BaseModel):
    session_id: str
    bucket: str = _DEFAULT_BUCKET
    reviewer_email: str | None = None
    force: bool = False
    ich_url: str | None = None


class ApproveRequest(BaseModel):
    session_id: str
    bucket: str = _DEFAULT_BUCKET
    folder_paths: list[str]
    therapeutic_area: str | None = None
    disease_type: str | None = None
    drug_name: str | None = None
    state: dict = {}


class DisapproveRequest(BaseModel):
    session_id: str
    feedback: str | None = None


class RefineRequest(BaseModel):
    session_id: str
    bucket: str = _DEFAULT_BUCKET
    feedback: str
    prior_feedback: str = ""
    ctd_output: dict | None = None
    ich_url: str | None = None


class CopyRequest(BaseModel):
    session_id: str
    bucket: str = _DEFAULT_BUCKET
    therapeutic_area: str
    disease_type: str
    drug_name: str
    state: dict = {}


class StatusResponse(BaseModel):
    extraction: dict = {}
    content: dict = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Internal: GCS helpers  (moved verbatim from app.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _slug(s: str) -> str:
    """Normalise a label into a GCS-safe path segment (lowercase, spaces → underscores)."""
    return s.strip().lower().replace(" ", "_")

def _get_bucket(name: str) -> storage.Bucket:
    """Return a GCS Bucket handle for the given bucket name (no network call made here)."""
    return _gcs_client.bucket(name)

def _gcs_session_path(session_id: str) -> str:
    """GCS blob path where per-session state (approved, ctd_output, content_program) is stored."""
    return f"ctd_structure/users/{session_id}/session_state.json"

def _gcs_status_path(session_id: str) -> str:
    """GCS blob path where the extraction job status (running/done/failed/timed_out) is stored."""
    return f"ctd_structure/users/{session_id}/extraction_status.json"

def _gcs_user_ctd_prefix(session_id: str) -> str:
    """GCS prefix under which a user’s draft CTD .keep blobs are written during extract/refine."""
    return f"ctd_structure/users/{session_id}/ctd"

def _program_prefix(ta: str, dis: str, drug: str) -> str:
    """Build the root GCS prefix for a drug program (e.g. therapeutic-area/oncology/lung_cancer/carboplatin)."""
    return f"{_GCS_PROGRAMS}/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}"


def _load_cached_paths(bucket_name: str, session_id: str | None = None) -> list[str]:
    """Return sorted CTD folder paths from GCS .keep blobs.

    Checks the user’s per-session draft prefix first (when session_id is given),
    then falls back to the shared canonical template at _GCS_TEMPLATE.
    Returns [] when neither location contains any .keep blobs.
    """
    try:
        bkt = _get_bucket(bucket_name)
        candidates: list[tuple[str, int]] = []
        if session_id:
            user_pfx = f"{_gcs_user_ctd_prefix(session_id)}/"
            candidates.append((user_pfx, len(user_pfx)))
        candidates.append((f"{_GCS_TEMPLATE}/", len("ctd_structure/ctd/")))
        for prefix, strip_len in candidates:
            paths = []
            for blob in bkt.list_blobs(prefix=prefix):
                if blob.name.endswith("/.keep"):
                    rel = blob.name[strip_len:]
                    paths.append(rel[: -len(".keep")])
            if paths:
                return sorted(paths)
        return []
    except Exception:
        return []


def _load_canonical_paths(bucket_name: str) -> list[str]:
    """Return the shared canonical CTD folder paths (no user-session prefix)."""
    return _load_cached_paths(bucket_name, session_id=None)


def _save_to_gcs(bucket_name: str, folder_paths: list[str]) -> None:
    """Write .keep marker blobs for every folder path into the shared canonical template location."""
    bkt = _get_bucket(bucket_name)
    scaffold_in_gcs(bkt, _GCS_TEMPLATE, folder_paths)


def _save_draft_to_gcs(bucket_name: str, session_id: str, folder_paths: list[str]) -> None:
    """Write .keep marker blobs into the user’s per-session draft CTD prefix."""
    bkt = _get_bucket(bucket_name)
    scaffold_in_gcs(bkt, _gcs_user_ctd_prefix(session_id), folder_paths)


def _save_session_state(bucket_name: str, state: dict) -> None:
    """Persist the fields that need to survive a page-reload (approved, ctd_output,
    content_program, content_run_id) to GCS as JSON.  Ephemeral UI state is not stored.
    """
    session_id = (state or {}).get("session_id", "default")
    try:
        payload = {
            "approved":                state.get("approved", False),
            "canonical_exists":        state.get("canonical_exists", False),
            "program_scaffold_exists": state.get("program_scaffold_exists", False),
            "ctd_output":              state.get("ctd_output"),
            "content_program":         state.get("content_program"),
            "content_run_id":          state.get("content_run_id"),
        }
        _get_bucket(bucket_name).blob(_gcs_session_path(session_id)).upload_from_string(
            json.dumps(payload), content_type="application/json",
            timeout=15,  # Prevent indefinite hang if GCS throttles after bulk scaffold writes
        )
    except Exception as exc:
        print(f"[api] Warning: could not save session state: {exc}")


def _load_session_state(bucket_name: str, session_id: str) -> dict:
    """Load the persisted session state from GCS.  Returns {} if the blob does not exist or on error."""
    try:
        blob = _get_bucket(bucket_name).blob(_gcs_session_path(session_id))
        if not blob.exists():
            return {}
        return json.loads(blob.download_as_text())
    except Exception:
        return {}


def _write_extraction_status(bucket_name: str, session_id: str, payload: dict) -> None:
    """Write an extraction job status dict to GCS, automatically stamping updated_at."""
    try:
        payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
        _get_bucket(bucket_name).blob(_gcs_status_path(session_id)).upload_from_string(
            json.dumps(payload), content_type="application/json"
        )
    except Exception as exc:
        print(f"[api] Warning: could not write extraction status: {exc}")


def _load_extraction_status(bucket_name: str, session_id: str) -> dict:
    """Load the extraction job status from GCS.

    If the job has been in 'running' or 'refining' state longer than
    _RUNNING_TIMEOUT_SECONDS it is treated as timed-out and the caller
    receives {"status": "timed_out", "age_minutes": N}.  Returns {} when
    no status blob exists.
    """
    try:
        raw = _get_bucket(bucket_name).blob(_gcs_status_path(session_id)).download_as_text()
        job = json.loads(raw)
    except Exception:
        return {}
    if job.get("status") in ("running", "refining"):
        updated_at = job.get("updated_at")
        if updated_at:
            try:
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(updated_at)).total_seconds()
                if age > _RUNNING_TIMEOUT_SECONDS:
                    return {"status": "timed_out", "age_minutes": round(age / 60)}
            except Exception:
                pass
    return job


# NOTE: Content status is owned by clinical-analyst (POST /trigger, GET /content_status).
# This read-only helper exists here solely to serve the combined GET /status endpoint,
# which the Gradio auto-poll timer calls every few seconds to get both extraction status
# and content status in a single round-trip.  No content logic runs here — it only reads
# the `content_status/latest.json` blob that content_worker writes to GCS.
def _load_content_status(bucket: str, ta: str, dis: str, drug: str) -> dict:
    """Read the content_worker’s status blob for a drug program from GCS.

    Tries content_status/latest.json first for speed; falls back to scanning
    all blobs under content_status/ and caching the newest one as latest.json.
    If the job has been 'running' longer than _CONTENT_STATUS_TIMEOUT_SECONDS
    it is returned as {"status": "timed_out", "age_minutes": N}.
    Returns {} when no status blob is found at all.
    """
    prefix = f"{_program_prefix(ta, dis, drug)}/content_status/"
    bkt_obj = _gcs_client.bucket(bucket)
    try:
        raw = bkt_obj.blob(f"{prefix}latest.json").download_as_text()
        job = json.loads(raw)
    except Exception:
        job = None
    if job is None:
        try:
            blobs = sorted(
                bkt_obj.list_blobs(prefix=prefix),
                key=lambda b: b.updated or b.time_deleted,
                reverse=True,
            )
            for blob in blobs:
                if blob.name.endswith(".json"):
                    job = json.loads(blob.download_as_text())
                    try:
                        bkt_obj.blob(f"{prefix}latest.json").upload_from_string(
                            json.dumps(job), content_type="application/json"
                        )
                    except Exception:
                        pass
                    break
        except Exception:
            pass
    if job is None:
        return {}
    if job.get("status") == "running":
        updated_at = job.get("updated_at")
        if updated_at:
            try:
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(updated_at)).total_seconds()
                if age > _CONTENT_STATUS_TIMEOUT_SECONDS:
                    return {"status": "timed_out", "age_minutes": round(age / 60)}
            except Exception:
                pass
    return job


def _check_program_exists(bucket: str, ta: str, dis: str, drug: str) -> bool:
    """Return True if the program’s CTD scaffold already exists in GCS (at least one blob found)."""
    prefix = f"{_program_prefix(ta, dis, drug)}/ctd/"
    try:
        return len(list(_gcs_client.bucket(bucket).list_blobs(prefix=prefix, max_results=1))) > 0
    except Exception:
        return False


def _publish_extraction(bucket: str, ich_url: str,
                        reviewer_email: str | None, session_id: str) -> str:
    """Publish an extraction job to the ctd-extraction Pub/Sub topic and return the run_id.

    The extractor worker (subscribed to the topic) will query the ICH index at ich_url,
    build the full ICH M4(R4) folder hierarchy, and write the result back to GCS.
    """
    run_id    = str(uuid.uuid4())
    publisher = pubsub_v1.PublisherClient()
    topic     = publisher.topic_path(_GCP_PROJECT, _PUBSUB_TOPIC)
    payload   = json.dumps({
        "bucket":         bucket,
        "ich_url":        ich_url,
        "reviewer_email": reviewer_email,
        "session_id":     session_id,
        "run_id":         run_id,
    }).encode()
    publisher.publish(topic, payload).result(timeout=10)
    return run_id


def _fmt_tree_paths(paths: list[str]) -> str:
    """Format a list of GCS folder paths into a human-readable indented tree with summary counts."""
    lines = []
    for p in paths:
        depth = p.rstrip("/").count("/") - 1
        name  = p.rstrip("/").rsplit("/", 1)[-1]
        lines.append("  " * depth + name + "/")
    n_mod = sum(1 for p in paths if p.count("/") == 2)
    n_sec = sum(1 for p in paths if p.count("/") == 3)
    n_sub = sum(1 for p in paths if p.count("/") == 4)
    lines.append(f"\n{n_mod} modules \u00b7 {n_sec} sections \u00b7 {n_sub} subsections  ({len(paths)} total paths)")
    return "\n".join(lines)


# # ═══════════════════════════════════════════════════════════════════════════════
# Internal: action handlers  (scoped to CTD scaffolding only)
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_extract(state: dict, bucket: str, reviewer_email: str | None,
                    force: bool = False) -> tuple[str, dict]:
    """Returns (reply_markdown, state_patch)."""
    session_id = state.get("session_id", "default")
    paths = _load_cached_paths(bucket, session_id)

    if paths and not force:
        session  = _load_session_state(bucket, session_id)
        tree     = _fmt_tree_paths(paths)
        patch = {
            "folder_paths":         paths,
            "ctd_output":           session.get("ctd_output"),
            "approved":             session.get("approved", False),
            "awaiting_feedback":    False,
            "disapproval_feedback": None,
        }
        return (
            "I found a cached structure in GCS — no need to re-query the ICH index.\n\n"
            f"```\n{tree}\n```\n\n"
            "You can copy it to a program now, or say **approve** to commit it as the "
            "shared default canonical template. Say **re-extract** to rebuild from scratch.",
            patch,
        )

    job = _load_extraction_status(bucket, session_id)
    if job.get("status") == "running":
        return (
            "⏳ Extraction is already running in the background.\n\n"
            "Ask me **status** to check progress, or refresh the page once it's done.",
            {},
        )

    ich_url = state.get("ich_url") or _DEFAULT_URL
    run_id  = _publish_extraction(bucket, ich_url, reviewer_email, session_id)
    _write_extraction_status(bucket, session_id, {"status": "running", "run_id": run_id})

    patch = {
        "awaiting_feedback":    False,
        "disapproval_feedback": None,
        "approved":             False,
        "folder_paths":         [],
    }
    return (
        "🚀 Extraction started in the **background** — querying the ICH index for the full "
        "ICH M4(R4) module → section → subsection hierarchy.\n\n"
        "⏳ This takes **3–8 minutes**. You can:\n"
        "- Ask me **status** at any time to check progress\n"
        "- **Refresh the page** when done — the completed structure will load automatically",
        patch,
    )


def _handle_approve(decision: CoordinatorDecision, state: dict, bucket: str) -> tuple[str, dict]:
    """Commit the current folder_paths as the shared canonical CTD template in GCS.

    If therapeutic_area / disease_type / drug_name are already known from the decision,
    immediately chains into _handle_copy to scaffold the program directory too.
    Returns (reply_markdown, state_patch).
    """
    folder_paths = state.get("folder_paths", [])
    if not folder_paths:
        return (
            "There's no structure loaded yet. Ask me to **extract** it first.",
            {},
        )
    try:
        _save_to_gcs(bucket, folder_paths)
    except Exception as exc:
        print(f"[api] Warning: GCS canonical persist on approve: {exc}")

    patch = {"approved": True, "canonical_exists": True}
    _save_session_state(bucket, {**state, **patch})

    if decision.therapeutic_area and decision.disease_type and decision.drug_name:
        copy_reply, copy_patch = _handle_copy(decision, {**state, **patch}, bucket)
        return copy_reply, {**patch, **copy_patch}

    canonical_note = (
        f"✅ Structure **committed as the shared canonical template** at "
        f"`gs://{bucket}/{_GCS_TEMPLATE}/`.\n\n"
        "All future users will load this as the default structure."
    )
    return (
        f"{canonical_note}\n\n"
        "To copy the structure into a program directory I need three things:\n\n"
        "| Field | Example |\n|---|---|\n"
        "| **Therapeutic area** | oncology, neurology, cardiology … |\n"
        "| **Disease / indication** | lung cancer, bells palsy … |\n"
        "| **Drug / compound name** | carboplatin, prednisolone … |\n\n"
        f"The structure will be placed at:\n"
        f"`gs://{bucket}/therapeutic-area/<area>/<disease>/<drug>/ctd/`\n\n"
        "> _Example: \"set up for oncology / lung cancer / carboplatin\"_",
        patch,
    )


def _handle_disapprove(decision: CoordinatorDecision, state: dict) -> tuple[str, dict]:
    """Record user disapproval and prompt for specific feedback to guide a targeted re-query.

    Sets awaiting_feedback=True so the next user message is routed to _handle_refine.
    Returns (reply_markdown, state_patch).
    """
    feedback = (decision.feedback or "").strip()
    if feedback:
        reply = (
            f"Understood — I caught this concern:\n\n> _{feedback}_\n\n"
            "If that captures it, just say **go ahead** and I'll re-query the "
            "ICH index for those specific areas. Or add more detail and I'll include that too."
        )
    else:
        reply = (
            "Got it — can you tell me what looks wrong or incomplete? For example:\n\n"
            "- *Module 5 sections are missing*\n"
            "- *Subsections under 2.5 Clinical Overview look incomplete*\n"
            "- *I expected more entries in Module 3*\n\n"
            "I'll use your feedback to do a targeted re-query of the ICH index."
        )
    patch = {
        "awaiting_feedback":    True,
        "disapproval_feedback": feedback,
        "approved":             False,
    }
    return reply, patch


def _handle_refine(feedback: str, state: dict, bucket: str) -> tuple[str, dict]:
    """Synchronous refinement — returns final reply + state patch."""
    prior    = (state.get("disapproval_feedback") or "").strip()
    combined = " ".join(filter(None, [prior, feedback])).strip() or "general review"
    session_id = state.get("session_id", "default")

    _write_extraction_status(bucket, session_id, {"status": "refining"})

    raw_output = state.get("ctd_output")
    if not raw_output:
        _write_extraction_status(bucket, session_id, {"status": "idle"})
        return (
            "❌ No previous structure found in session. Please run a full **extract** first.",
            {"awaiting_feedback": False},
        )

    try:
        current_output = CTDStructureOutput(**raw_output)
    except Exception as exc:
        _write_extraction_status(bucket, session_id, {"status": "idle"})
        return (
            f"❌ Could not deserialise stored structure: {exc}",
            {"awaiting_feedback": False},
        )

    ich_url = state.get("ich_url") or _DEFAULT_URL
    try:
        output, evaluation = refine_from_feedback(ich_url, combined, current_output)
    except Exception as exc:
        _write_extraction_status(bucket, session_id, {"status": "failed", "error": str(exc)})
        return (
            f"❌ Refinement query failed: {exc}\n\nTry a full **re-extract**.",
            {"awaiting_feedback": False},
        )

    folder_paths = output.to_folder_paths()

    try:
        _save_draft_to_gcs(bucket, session_id, folder_paths)
    except Exception as exc:
        print(f"[api] Warning: GCS persist after refine: {exc}")

    patch = {
        "awaiting_feedback":    False,
        "folder_paths":         folder_paths,
        "ctd_output":           output.model_dump(),
    }
    _save_session_state(bucket, {**state, **patch})
    _write_extraction_status(bucket, session_id, {"status": "done", "n_paths": len(folder_paths)})

    from ctd_structure.structure import _fmt_tree_output  # type: ignore[attr-defined]
    tree = _fmt_tree_output(output, evaluation) if hasattr(output, "modules") else _fmt_tree_paths(folder_paths)

    eval_note = ""
    if not evaluation.passed:
        issues = "\n".join(
            f"  • [{i.level.upper()}] {i.path}: {i.reason}"
            for i in evaluation.issues
        )
        eval_note = f"\n\n**Remaining issues after re-query:**\n{issues}"

    reply = (
        f"✅ Refinement complete — **{len(folder_paths)} folder paths** assembled.\n\n"
        f"```\n{tree}\n```"
        f"{eval_note}\n\n"
        "You can copy this to a program now, or say **approve** to commit it as the "
        "shared default canonical template."
    )
    return reply, patch


def _handle_copy(decision: CoordinatorDecision, state: dict, bucket: str) -> tuple[str, dict]:
    """Scaffold the shared canonical CTD folder tree into a drug-program directory in GCS.

    Reads the canonical paths from _GCS_TEMPLATE and calls scaffold_in_gcs to write
    .keep blobs under therapeutic-area/<ta>/<dis>/<drug>/ctd/.  Sets
    program_scaffold_exists=True in state_patch so downstream steps know the directory exists.
    Returns (reply_markdown, state_patch).
    """
    ta   = decision.therapeutic_area.strip().lower().replace(" ", "_")
    dis  = decision.disease_type.strip().lower().replace(" ", "_")
    drug = decision.drug_name.strip().lower().replace(" ", "_")
    gcs_base = f"{_GCS_PROGRAMS}/{ta}/{dis}/{drug}/ctd"

    canonical_paths = _load_canonical_paths(bucket)
    if not canonical_paths:
        return (
            "❌ No default CTD structure has been established yet.\n\n"
            "To set it up:\n"
            "1. Say **extract** to build the ICH M4(R4) hierarchy from the ICH index\n"
            "2. Say **approve** to publish it as the shared default\n\n"
            "Once established, anyone can scaffold programs from it instantly.",
            {},
        )

    try:
        scaffold_in_gcs(_get_bucket(bucket), gcs_base, canonical_paths)
    except Exception as exc:
        return f"❌ GCS copy failed: {exc}", {}

    patch = {
        "program_scaffold_exists": True,
        "canonical_exists":        True,
        "content_program": {
            "therapeutic_area": decision.therapeutic_area.strip(),
            "disease_type":     decision.disease_type.strip(),
            "drug_name":        decision.drug_name.strip(),
        },
    }
    _save_session_state(bucket, {**state, **patch})
    return (
        f"✅ Done! The default CTD structure has been scaffolded at:\n\n"
        f"`gs://{bucket}/{gcs_base}/`\n\n"
        f"**{len(canonical_paths)} folder markers** written for "
        f"**{ta.replace('_',' ')} / {dis.replace('_',' ')} / {drug.replace('_',' ')}**.\n\n"
        "The CTD folder hierarchy is ready. Say **generate content** when you're ready to "
        "kick off the 3-pass content generation pipeline.",
        patch,
    )


def _handle_status(state: dict, bucket: str, msg_lower: str) -> tuple[str, dict]:
    """Build a human-readable status reply covering both extraction and content generation.

    Priority order:
      1. If content_program is set → report content_worker progress (running/done/failed).
      2. Else if 'content' appears in msg_lower → prompt user to scaffold a program first.
      3. Else → report extraction job status (running/refining/done/failed/timed_out/idle).
    Returns (reply_markdown, state_patch).
    """
    session_id = state.get("session_id", "default")
    job        = _load_extraction_status(bucket, session_id)

    prog = state.get("content_program")
    if prog:
        ta_c, dis_c, drug_c = prog.get("therapeutic_area", ""), prog.get("disease_type", ""), prog.get("drug_name", "")
        cjob    = _load_content_status(bucket, ta_c, dis_c, drug_c)
        cstatus = cjob.get("status")
        if cstatus == "running":
            pass_label = cjob.get("pass_label", "")
            pass_num   = cjob.get("pass_number", "")
            total      = cjob.get("total_passes", 3)
            step_info  = f"Pass {pass_num}/{total}: **{pass_label}**" if pass_label else "Initialising…"
            return (
                f"⏳ **Content generation is running** — {step_info}\n\n"
                "Ask me **status** again in a minute to check progress.",
                {},
            )
        elif cstatus == "done":
            return (
                "✅ **Content generation is complete.** "
                "All three passes (Module 5 → 2.7 → 2.5 overviews) finished successfully.\n\n"
                "Say **generate content** to regenerate with updated clinical data.",
                {"content_run_id": None},
            )
        elif cstatus == "failed":
            return (
                f"❌ **Content generation failed** — {cjob.get('error', 'unknown')}\n\n"
                "Say **generate content** to re-run.",
                {"content_run_id": None},
            )
        else:
            return (
                f"No content generation job has been queued yet for "
                f"**{ta_c} / {dis_c} / {drug_c}**.\n\n"
                "Say **generate content** to start the 3-pass pipeline.",
                {},
            )
    elif "content" in msg_lower:
        return (
            "No content program is set up in this session yet.\n\n"
            "Please scaffold a program first:\n\n"
            "> *Copy the structure to \\<therapeutic area\\> / \\<disease\\> / \\<drug\\>*\n\n"
            "Then say **generate content** to queue the 3-pass pipeline.",
            {},
        )

    # Extraction status
    status = job.get("status")
    if status == "running":
        return "⏳ **Not yet** — extraction is still running in the background.\n\nAsk me **status** again in a moment.", {}
    elif status == "refining":
        return "⏳ **Targeted repair is running** — re-querying the ICH index for the affected modules.", {}
    elif status == "done":
        paths = _load_cached_paths(bucket, session_id)
        if paths:
            tree = _fmt_tree_paths(paths)
            return (
                "✅ **Extraction is complete!**\n\n"
                f"```\n{tree}\n```\n\n"
                "You can copy it to a program now.",
                {"folder_paths": paths},
            )
        return "✅ Extraction finished, but paths could not be read from GCS — try refreshing.", {}
    elif status == "failed":
        return f"❌ **Extraction failed** — {job.get('error', 'unknown error')}\n\nSay **re-extract** to try again.", {}
    elif status == "timed_out":
        age = job.get("age_minutes", "?")
        _write_extraction_status(bucket, session_id, {"status": "idle"})
        return (
            f"⚠️ **Job timed out** — the background job started {age} minutes ago but never finished.\n\n"
            "Say **re-extract** to start a fresh extraction.",
            {},
        )
    else:
        paths = state.get("folder_paths", [])
        approved = state.get("approved", False)
        if paths:
            note = "✅ Already approved." if approved else "Not yet approved — say **approve** when ready."
            return f"✅ **Structure is loaded** ({len(paths)} folder paths). {note}", {}
        return (
            "No extraction is running and no structure is loaded yet.\n\n"
            "Say **extract** to build the ICH M4(R4) CTD folder structure from the ICH index.",
            {},
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# Action endpoints  (coordinator lives in ui/ — these are pure operations)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/extract", response_model=ActionResponse)
def extract(req: ExtractRequest) -> ActionResponse:
    """Publish a background extraction job to Pub/Sub.  The worker queries the ICH index
    and writes the resulting CTD folder hierarchy back to GCS when done.
    """
    state = {"session_id": req.session_id, "ich_url": req.ich_url}
    reply, patch = _handle_extract(state, req.bucket, req.reviewer_email, req.force)
    return ActionResponse(reply=reply, state_patch=patch)


@app.post("/approve", response_model=ActionResponse)
def approve(req: ApproveRequest) -> ActionResponse:
    """Commit the supplied folder_paths as the shared canonical CTD template.
    Optionally auto-copies into a program directory if ta/dis/drug are supplied.
    """
    decision = CoordinatorDecision(
        outcome="proceed", intent="approve",
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    state = {**req.state, "session_id": req.session_id, "folder_paths": req.folder_paths}
    reply, patch = _handle_approve(decision, state, req.bucket)
    return ActionResponse(reply=reply, state_patch=patch)


@app.post("/disapprove", response_model=ActionResponse)
def disapprove(req: DisapproveRequest) -> ActionResponse:
    """Signal that the user is unhappy with the extracted structure and prompt for feedback."""
    decision = CoordinatorDecision(outcome="proceed", intent="disapprove", feedback=req.feedback)
    state = {"session_id": req.session_id}
    reply, patch = _handle_disapprove(decision, state)
    return ActionResponse(reply=reply, state_patch=patch)


@app.post("/refine", response_model=ActionResponse)
def refine(req: RefineRequest) -> ActionResponse:
    """Re-query the ICH index using the user’s feedback to repair the extracted structure.
    Runs synchronously; returns the updated folder tree when done.
    """
    state = {
        "session_id":           req.session_id,
        "disapproval_feedback": req.prior_feedback,
        "ctd_output":           req.ctd_output,
        "ich_url":              req.ich_url,
    }
    reply, patch = _handle_refine(req.feedback, state, req.bucket)
    return ActionResponse(reply=reply, state_patch=patch)


@app.post("/copy", response_model=ActionResponse)
def copy(req: CopyRequest) -> ActionResponse:
    """Scaffold the canonical CTD folder tree into the specified drug-program GCS directory."""
    decision = CoordinatorDecision(
        outcome="proceed", intent="copy",
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    state = {**req.state, "session_id": req.session_id}
    reply, patch = _handle_copy(decision, state, req.bucket)
    return ActionResponse(reply=reply, state_patch=patch)


@app.get("/status_query", response_model=ActionResponse)
def status_query(
    session_id: str,
    bucket: str = _DEFAULT_BUCKET,
    therapeutic_area: str | None = None,
    disease_type: str | None = None,
    drug_name: str | None = None,
    msg_lower: str = "status",
) -> ActionResponse:
    """Returns a human-readable reply string, used by the UI's chat handler."""
    state = {
        "session_id": session_id,
        "content_program": {"therapeutic_area": therapeutic_area, "disease_type": disease_type, "drug_name": drug_name} if therapeutic_area and disease_type and drug_name else None,
        "folder_paths": [],
        "approved": False,
    }
    reply, patch = _handle_status(state, bucket, msg_lower)
    return ActionResponse(reply=reply, state_patch=patch)


@app.get("/health")
def health():
    """Liveness probe — returns 200 OK if the service is up."""
    return {"status": "ok"}


@app.get("/status", response_model=StatusResponse)
def status(session_id: str, bucket: str = _DEFAULT_BUCKET,
           therapeutic_area: str | None = None, disease_type: str | None = None, drug_name: str | None = None):
    """Poll extraction + content-generation status (used by the auto-poll timer in Gradio).

    Returns both extraction and content state in one call to avoid two separate round-trips
    from the UI timer.  Content state is read-only — triggering lives in clinical-analyst.
    """
    extraction = _load_extraction_status(bucket, session_id)
    content    = _load_content_status(bucket, therapeutic_area or "", disease_type or "", drug_name or "") if therapeutic_area and disease_type and drug_name else {}
    return StatusResponse(extraction=extraction, content=content)


@app.get("/validation-report")
def validation_report(
    therapeutic_area: str,
    disease_type: str,
    drug_name: str,
    bucket: str = _DEFAULT_BUCKET,
):
    """Return the latest consistency validation report for a drug program.

    Reads `content_status/validation_report.json` that the content_worker saves
    after every successful generation run.  Returns {} when no report exists yet.
    """
    prefix = _program_prefix(therapeutic_area, disease_type, drug_name)
    blob_path = f"{prefix}/content_status/validation_report.json"
    try:
        raw = _gcs_client.bucket(bucket).blob(blob_path).download_as_text()
        return json.loads(raw)
    except Exception:
        return {}


@app.get("/session")
def get_session(session_id: str, bucket: str = _DEFAULT_BUCKET):
    """Return session state + cached folder paths (used by Gradio on page load)."""
    paths     = _load_cached_paths(bucket, session_id)
    canonical = _load_canonical_paths(bucket)
    session   = _load_session_state(bucket, session_id)
    return {
        "folder_paths":             paths,
        "canonical_exists":         bool(canonical),
        "approved":                 session.get("approved", False),
        "program_scaffold_exists":  session.get("program_scaffold_exists", False),
        "ctd_output":               session.get("ctd_output"),
        "content_program":          session.get("content_program"),
        "content_run_id":           session.get("content_run_id"),
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8081))
    uvicorn.run("api_app:app", host="0.0.0.0", port=port, reload=False)
