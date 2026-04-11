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
  POST /write            → publish content-generation job to Pub/Sub
  GET  /status_query     → extraction + content status as a reply string
  GET  /status           → raw extraction + content status dicts (for auto-poll)
  GET  /session          → load session state + folder paths
  POST /upload_clinical  → register a clinical CSV for a program
  GET  /health           → liveness probe
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
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

# Clinical ingestion — optional (may not be present in minimal API image)
try:
    from clinical.ingestion import register_clinical_csv as _register_csv
    from template.models import ProgramInfo as _ProgramInfo
    _CLINICAL_AVAILABLE = True
except ImportError:
    _CLINICAL_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_URL    = os.environ.get("ICH_INDEX_URL",
                                 "https://ich4-index-811317821863.us-central1.run.app")
_DEFAULT_BUCKET = os.environ.get("GCS_BUCKET",
                                 "pharma-reguatory-author-life-science")
_GCS_TEMPLATE   = "ctd_structure/ctd"
_GCS_PROGRAMS   = "therapeutic-area"
_PUBSUB_TOPIC         = os.environ.get("PUBSUB_TOPIC", "ctd-extraction")
_CONTENT_PUBSUB_TOPIC = os.environ.get("CONTENT_PUBSUB_TOPIC", "ich4-content-generation")
_GCP_PROJECT    = os.environ.get("GCP_PROJECT_ID", "pharma-reguatory-author")
_RUNNING_TIMEOUT_SECONDS        = 20 * 60   # 20 min
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


class WriteRequest(BaseModel):
    session_id: str
    bucket: str = _DEFAULT_BUCKET
    therapeutic_area: str
    disease_type: str
    drug_name: str
    program_scaffold_exists: bool = False
    force_no_clinical: bool = False  # True when user confirmed to proceed without clinical data
    state: dict = {}


class StatusResponse(BaseModel):
    extraction: dict = {}
    content: dict = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Internal: GCS helpers  (moved verbatim from app.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _slug(s: str) -> str:
    return s.strip().lower().replace(" ", "_")

def _get_bucket(name: str) -> storage.Bucket:
    return _gcs_client.bucket(name)

def _gcs_session_path(session_id: str) -> str:
    return f"ctd_structure/users/{session_id}/session_state.json"

def _gcs_status_path(session_id: str) -> str:
    return f"ctd_structure/users/{session_id}/extraction_status.json"

def _gcs_user_ctd_prefix(session_id: str) -> str:
    return f"ctd_structure/users/{session_id}/ctd"

def _program_prefix(ta: str, dis: str, drug: str) -> str:
    return f"{_GCS_PROGRAMS}/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}"


def _load_cached_paths(bucket_name: str, session_id: str | None = None) -> list[str]:
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
    return _load_cached_paths(bucket_name, session_id=None)


def _save_to_gcs(bucket_name: str, folder_paths: list[str]) -> None:
    bkt = _get_bucket(bucket_name)
    scaffold_in_gcs(bkt, _GCS_TEMPLATE, folder_paths)


def _save_draft_to_gcs(bucket_name: str, session_id: str, folder_paths: list[str]) -> None:
    bkt = _get_bucket(bucket_name)
    scaffold_in_gcs(bkt, _gcs_user_ctd_prefix(session_id), folder_paths)


def _save_session_state(bucket_name: str, state: dict) -> None:
    session_id = (state or {}).get("session_id", "default")
    try:
        payload = {
            "approved":        state.get("approved", False),
            "ctd_output":      state.get("ctd_output"),
            "content_program": state.get("content_program"),
            "content_run_id":  state.get("content_run_id"),
        }
        _get_bucket(bucket_name).blob(_gcs_session_path(session_id)).upload_from_string(
            json.dumps(payload), content_type="application/json"
        )
    except Exception as exc:
        print(f"[api] Warning: could not save session state: {exc}")


def _load_session_state(bucket_name: str, session_id: str) -> dict:
    try:
        blob = _get_bucket(bucket_name).blob(_gcs_session_path(session_id))
        if not blob.exists():
            return {}
        return json.loads(blob.download_as_text())
    except Exception:
        return {}


def _write_extraction_status(bucket_name: str, session_id: str, payload: dict) -> None:
    try:
        payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
        _get_bucket(bucket_name).blob(_gcs_status_path(session_id)).upload_from_string(
            json.dumps(payload), content_type="application/json"
        )
    except Exception as exc:
        print(f"[api] Warning: could not write extraction status: {exc}")


def _load_extraction_status(bucket_name: str, session_id: str) -> dict:
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


def _load_content_status(bucket: str, ta: str, dis: str, drug: str) -> dict:
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
    prefix = f"{_program_prefix(ta, dis, drug)}/ctd/"
    try:
        return len(list(_gcs_client.bucket(bucket).list_blobs(prefix=prefix, max_results=1))) > 0
    except Exception:
        return False


def _load_clinical_manifest(bucket: str, ta: str, dis: str, drug: str) -> dict | None:
    path = f"{_program_prefix(ta, dis, drug)}/clinical_data/manifest.json"
    try:
        raw = _gcs_client.bucket(bucket).blob(path).download_as_text()
        return json.loads(raw)
    except Exception:
        return None


def _publish_extraction(bucket: str, ich_url: str,
                        reviewer_email: str | None, session_id: str) -> str:
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


def _publish_content_generation(bucket: str, ta: str, dis: str,
                                  drug: str, session_id: str) -> str:
    run_id    = str(uuid.uuid4())
    publisher = pubsub_v1.PublisherClient()
    topic     = publisher.topic_path(_GCP_PROJECT, _CONTENT_PUBSUB_TOPIC)
    payload   = json.dumps({
        "bucket":    bucket,
        "session_id": session_id,
        "run_id":    run_id,
        "program": {
            "therapeutic_area": ta,
            "disease_type":     dis,
            "drug_name":        drug,
        },
    }).encode()
    publisher.publish(topic, payload).result(timeout=10)
    return run_id


def _fmt_tree_paths(paths: list[str]) -> str:
    lines = []
    for p in paths:
        depth = p.rstrip("/").count("/") - 1
        name  = p.rstrip("/").rsplit("/", 1)[-1]
        lines.append("  " * depth + name + "/")
    n_mod = sum(1 for p in paths if p.count("/") == 2)
    n_sec = sum(1 for p in paths if p.count("/") == 3)
    n_sub = sum(1 for p in paths if p.count("/") == 4)
    lines.append(f"\n{n_mod} modules · {n_sec} sections · {n_sub} subsections  ({len(paths)} total paths)")
    return "\n".join(lines)


def _summarise_clinical_manifest(manifest: dict) -> str:
    sources = manifest.get("sources", [])
    if not sources:
        return "_(manifest found but no datasets registered)_"
    lines = [f"📋 **{len(sources)} clinical dataset(s) registered:**\n"]
    for src in sources:
        cols    = src.get("column_mappings", [])
        secs    = src.get("ctd_section_keys", [])
        sec_str = ", ".join(sorted(secs)[:5]) or "—"
        lines.append(
            f"- **{src.get('filename', '?')}** — {src.get('study_type', '?')} study "
            f"({len(cols)} mapped columns → CTD sections: {sec_str})"
        )
        endpoints = [f"`{m['placeholder_key']}`" for m in cols[:6]]
        if endpoints:
            more = f" + {len(cols) - 6} more" if len(cols) > 6 else ""
            lines.append(f"  Endpoints mapped: {', '.join(endpoints)}{more}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Internal: action handlers  (moved verbatim from app.py's _do_* functions)
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
            "ta":   decision.therapeutic_area.strip(),
            "dis":  decision.disease_type.strip(),
            "drug": decision.drug_name.strip(),
        },
    }
    _save_session_state(bucket, {**state, **patch})
    return (
        f"✅ Done! The default CTD structure has been scaffolded at:\n\n"
        f"`gs://{bucket}/{gcs_base}/`\n\n"
        f"**{len(canonical_paths)} folder markers** written for "
        f"**{ta.replace('_',' ')} / {dis.replace('_',' ')} / {drug.replace('_',' ')}**.\n\n"
        "---\n"
        "**Before generating content, upload your clinical data:**\n\n"
        "| Step | Action |\n|---|---|\n"
        "| 1️⃣ | Open the **🔬 Clinical Data Upload** panel |\n"
        "| 2️⃣ | Upload your clinical trial CSV |\n"
        "| 3️⃣ | Say **generate content** to kick off the 3-pass generation |\n\n"
        "> _Without clinical data, all `{{placeholder}}` values will remain unfilled._",
        patch,
    )


def _handle_write(decision: CoordinatorDecision, state: dict, bucket: str) -> tuple[str, dict]:
    ta         = decision.therapeutic_area.strip()
    dis        = decision.disease_type.strip()
    drug       = decision.drug_name.strip()
    session_id = state.get("session_id", "default")

    # Auto-scaffold if needed
    if not state.get("program_scaffold_exists"):
        canonical_paths = _load_canonical_paths(bucket)
        if not canonical_paths:
            return (
                "❌ No default CTD structure has been established yet.\n\n"
                "Say **extract** to build it, then **approve** to publish it.",
                {},
            )
        try:
            scaffolded_base = f"{_GCS_PROGRAMS}/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}/ctd"
            scaffold_in_gcs(_get_bucket(bucket), scaffolded_base, canonical_paths)
            state = {**state, "program_scaffold_exists": True}
        except Exception as exc:
            return f"❌ Auto-scaffold failed: {exc}", {}

    manifest = _load_clinical_manifest(bucket, ta, dis, drug)
    if not manifest or not manifest.get("sources"):
        patch = {
            "awaiting_write_confirm": {"ta": ta, "dis": dis, "drug": drug},
        }
        return (
            f"⚠️ **No clinical data found** for **{ta} / {dis} / {drug}**.\n\n"
            "Without clinical data, all `{{placeholder}}` values will appear as "
            "`{{placeholder}} [NOT FILLED]` and will need manual editing.\n\n"
            "**Options:**\n"
            "- Say **yes, proceed** to generate content with empty placeholders.\n"
            "- Say **cancel** to stop and upload clinical data first.",
            patch,
        )

    summary = _summarise_clinical_manifest(manifest)
    run_id  = _publish_content_generation(bucket, ta, dis, drug, session_id)
    patch = {
        "content_run_id":  run_id,
        "content_program": {"ta": ta, "dis": dis, "drug": drug},
    }
    _save_session_state(bucket, {**state, **patch})
    return (
        f"{summary}\n\n"
        "✅ Content generation will use this real clinical data to fill in all placeholder values. "
        f"Queuing now…\n\n"
        f"⏳ Content generation queued for **{ta} / {dis} / {drug}** (run `{run_id[:8]}…`).\n\n"
        "The worker will:\n"
        "1. Query ICH guidelines (index service)\n"
        "2. Load the clinical datasets listed above\n"
        "3. Generate section templates and fill placeholders with real data\n"
        "4. Validate all CTD sections\n\n"
        "This usually takes 5–10 minutes. Ask me **content status** to check progress.",
        patch,
    )


def _handle_status(state: dict, bucket: str, msg_lower: str) -> tuple[str, dict]:
    session_id = state.get("session_id", "default")
    job        = _load_extraction_status(bucket, session_id)

    prog = state.get("content_program")
    if prog:
        ta_c, dis_c, drug_c = prog.get("ta", ""), prog.get("dis", ""), prog.get("drug", "")
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
    state = {"session_id": req.session_id, "ich_url": req.ich_url}
    reply, patch = _handle_extract(state, req.bucket, req.reviewer_email, req.force)
    return ActionResponse(reply=reply, state_patch=patch)


@app.post("/approve", response_model=ActionResponse)
def approve(req: ApproveRequest) -> ActionResponse:
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
    decision = CoordinatorDecision(outcome="proceed", intent="disapprove", feedback=req.feedback)
    state = {"session_id": req.session_id}
    reply, patch = _handle_disapprove(decision, state)
    return ActionResponse(reply=reply, state_patch=patch)


@app.post("/refine", response_model=ActionResponse)
def refine(req: RefineRequest) -> ActionResponse:
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
    decision = CoordinatorDecision(
        outcome="proceed", intent="copy",
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    state = {**req.state, "session_id": req.session_id}
    reply, patch = _handle_copy(decision, state, req.bucket)
    return ActionResponse(reply=reply, state_patch=patch)


@app.post("/write", response_model=ActionResponse)
def write(req: WriteRequest) -> ActionResponse:
    state = {
        **req.state,
        "session_id":              req.session_id,
        "program_scaffold_exists": req.program_scaffold_exists,
    }
    if req.force_no_clinical:
        run_id = _publish_content_generation(
            req.bucket, req.therapeutic_area, req.disease_type, req.drug_name, req.session_id
        )
        patch = {
            "awaiting_write_confirm": None,
            "content_run_id":  run_id,
            "content_program": {"ta": req.therapeutic_area, "dis": req.disease_type, "drug": req.drug_name},
        }
        _save_session_state(req.bucket, {**state, **patch})
        ta, dis, drug = req.therapeutic_area, req.disease_type, req.drug_name
        return ActionResponse(
            reply=(
                f"⏳ Content generation queued for **{ta} / {dis} / {drug}** "
                f"(run `{run_id[:8]}…`).\n\n"
                "⚠️ Placeholder values will appear as `{{key}} [NOT FILLED]` — "
                "edit manually after generation, or register clinical data and regenerate.\n\n"
                "Ask me **content status** to check progress."
            ),
            state_patch=patch,
        )
    decision = CoordinatorDecision(
        outcome="proceed", intent="write",
        therapeutic_area=req.therapeutic_area,
        disease_type=req.disease_type,
        drug_name=req.drug_name,
    )
    reply, patch = _handle_write(decision, state, req.bucket)
    return ActionResponse(reply=reply, state_patch=patch)


@app.get("/status_query", response_model=ActionResponse)
def status_query(
    session_id: str,
    bucket: str = _DEFAULT_BUCKET,
    ta: str | None = None,
    dis: str | None = None,
    drug: str | None = None,
    msg_lower: str = "status",
) -> ActionResponse:
    """Returns a human-readable reply string, used by the UI's chat handler."""
    state = {
        "session_id": session_id,
        "content_program": {"ta": ta, "dis": dis, "drug": drug} if ta and dis and drug else None,
        "folder_paths": [],
        "approved": False,
    }
    reply, patch = _handle_status(state, bucket, msg_lower)
    return ActionResponse(reply=reply, state_patch=patch)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status", response_model=StatusResponse)
def status(session_id: str, bucket: str = _DEFAULT_BUCKET,
           ta: str | None = None, dis: str | None = None, drug: str | None = None):
    """Poll extraction + content-generation status (used by the auto-poll timer in Gradio)."""
    extraction = _load_extraction_status(bucket, session_id)
    content    = _load_content_status(bucket, ta or "", dis or "", drug or "") if ta and dis and drug else {}
    return StatusResponse(extraction=extraction, content=content)


@app.get("/session")
def get_session(session_id: str, bucket: str = _DEFAULT_BUCKET):
    """Return session state + cached folder paths (used by Gradio on page load)."""
    paths     = _load_cached_paths(bucket, session_id)
    canonical = _load_canonical_paths(bucket)
    session   = _load_session_state(bucket, session_id)
    return {
        "folder_paths":    paths,
        "canonical_exists": bool(canonical),
        "approved":        session.get("approved", False),
        "ctd_output":      session.get("ctd_output"),
        "content_program": session.get("content_program"),
        "content_run_id":  session.get("content_run_id"),
    }


@app.post("/upload_clinical")
async def upload_clinical(
    csv_file: UploadFile = File(...),
    ta:     str = Form(...),
    dis:    str = Form(...),
    drug:   str = Form(...),
    bucket: str = Form(_DEFAULT_BUCKET),
):
    """Register an uploaded clinical CSV for a drug program."""
    if not _CLINICAL_AVAILABLE:
        raise HTTPException(status_code=501, detail="Clinical ingestion package not available.")

    import tempfile, shutil
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        shutil.copyfileobj(csv_file.file, tmp)
        tmp_path = tmp.name

    try:
        program  = _ProgramInfo(therapeutic_area=ta, disease_type=dis, drug_name=drug)
        manifest = _register_csv(
            bucket_name=bucket,
            program=program,
            local_csv_path=tmp_path,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        sources = manifest.sources
        src     = sources[-1] if sources else None
        return {
            "status":   "ok",
            "filename": src.filename if src else csv_file.filename,
            "columns":  len(src.column_mappings) if src else 0,
            "sections": src.ctd_section_keys if src else [],
            "total_sources": len(sources),
        }
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8081))
    uvicorn.run("api_app:app", host="0.0.0.0", port=port, reload=False)
