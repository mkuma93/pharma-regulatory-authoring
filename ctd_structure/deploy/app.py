"""
ctd_structure/deploy/app.py

Conversational Gradio UI for the CTD Structure workflow.

Flow
────
  On load   → check GCS for a cached canonical template.
              If found  → show the folder tree, ask for approval.
              If absent → tell user to ask for extraction.

  User says "extract / generate / build…"
            → skip if GCS cache exists (already shown on load).
            → otherwise run the LangGraph pipeline, show tree, ask for approval.

  User says "approve / looks good / yes…"
            → persist template to GCS, mark approved, ask where to copy.

  User says "copy to <area> / <disease> / <drug>"  (natural language)
            → require approval first, then scaffold the program folder in GCS.

Environment variables (set by Cloud Run via cloudbuild.yaml):
  ICH_INDEX_URL  — base URL of the ICH4/index service
  GCS_BUCKET     — destination bucket
  PORT           — port to bind (injected by Cloud Run)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import gradio as gr
from google.cloud import pubsub_v1
from google.cloud import storage

_here      = os.path.dirname(os.path.abspath(__file__))
_pkg_root  = os.path.dirname(_here)
_workspace = os.path.dirname(_pkg_root)
for _p in [_workspace, _pkg_root]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ctd_structure.scaffold import scaffold_in_gcs  # noqa: E402
from ctd_structure.structure import (
    CTDStructureOutput,
    EvaluationResult,
    refine_from_feedback,
)
from main import CoordinatorDecision, IntentResult, parse_intent, run_coordinator

# Clinical data ingestion — available when clinical/ package is in PYTHONPATH
try:
    from clinical.ingestion import register_clinical_csv as _register_csv
    from template.models import ProgramInfo as _ProgramInfo
    _CLINICAL_UPLOAD_AVAILABLE = True
except ImportError:
    _CLINICAL_UPLOAD_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_URL    = os.environ.get("ICH_INDEX_URL",
                                 "https://ich4-index-74ugcbbbya-uc.a.run.app")
_DEFAULT_BUCKET = os.environ.get("GCS_BUCKET",
                                 "pharma-reguatory-author-life-science")
_GCS_TEMPLATE  = "ctd_structure/ctd"
_GCS_PROGRAMS  = "therapeutic-area"
_PUBSUB_TOPIC         = os.environ.get("PUBSUB_TOPIC", "ctd-extraction")
_CONTENT_PUBSUB_TOPIC = os.environ.get("CONTENT_PUBSUB_TOPIC", "ich4-content-generation")
_GCP_PROJECT   = os.environ.get("GCP_PROJECT_ID", "pharma-reguatory-author")
_CONTENT_STATUS_TIMEOUT_SECONDS = 30 * 60  # 30 min
_COMPLIANCE_OFFICERS = {
    e.strip().lower()
    for e in os.environ.get("COMPLIANCE_OFFICER_EMAILS", "mritunjay.kmr1@gmail.com").split(",")
    if e.strip()
}

def _gcs_session_path(session_id: str) -> str:
    return f"ctd_structure/users/{session_id}/session_state.json"

def _gcs_status_path(session_id: str) -> str:
    return f"ctd_structure/users/{session_id}/extraction_status.json"

def _gcs_user_ctd_prefix(session_id: str) -> str:
    """Per-user draft CTD prefix — isolated from every other user's work."""
    return f"ctd_structure/users/{session_id}/ctd"

def _content_status_path(ta: str, dis: str, drug: str, session_id: str = "") -> str:
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_")
    suffix = session_id.strip() if session_id and session_id.strip() else "latest"
    return (
        f"{_GCS_PROGRAMS}/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}"
        f"/content_status/{suffix}.json"
    )

def _publish_content_generation(
    bucket: str,
    ta: str,
    dis: str,
    drug: str,
    session_id: str = "default",
) -> str:
    """Publish a content-generation request to the ich4-content-generation topic.
    Returns the run_id."""
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
    }).encode("utf-8")
    publisher.publish(topic, payload).result(timeout=10)
    return run_id

def _load_content_status(bucket: str, ta: str, dis: str, drug: str, session_id: str) -> dict:
    """Read content-generation job status from GCS. Returns {} if not found.

    Tries latest.json first; falls back to any existing session-scoped status
    file (legacy, written before the latest.json migration) and migrates it.
    """
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_")
    prefix = (
        f"{_GCS_PROGRAMS}/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}/content_status/"
    )
    bkt_obj = _gcs_client.bucket(bucket)

    # Primary: latest.json (written by updated worker)
    try:
        raw = bkt_obj.blob(f"{prefix}latest.json").download_as_text()
        job = json.loads(raw)
    except Exception:
        job = None

    # Fallback: any session-scoped file (legacy)
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
                    # Migrate to latest.json so future reads are fast
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
    if job.get("status") in ("running",):
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

# Intent classification is in main.py — IntentResult and parse_intent imported above.

# ── GCS helpers ───────────────────────────────────────────────────────────────

_gcs_client = storage.Client()

def _get_bucket(bucket_name: str) -> storage.Bucket:
    return _gcs_client.bucket(bucket_name)

def _load_cached_paths(bucket_name: str, session_id: str | None = None) -> list[str]:
    """
    Return folder paths for the given session.
    Priority: user's own draft (ctd_structure/users/{session_id}/ctd/)  →
              shared canonical template (ctd_structure/ctd/).
    """
    try:
        bkt = _get_bucket(bucket_name)
        # Build list of (prefix, chars_to_strip) in priority order
        candidates: list[tuple[str, int]] = []
        if session_id:
            user_pfx = f"{_gcs_user_ctd_prefix(session_id)}/"
            # Strip up to and including the trailing "/" so returned paths are
            # relative to the user's ctd base, matching to_folder_paths() output
            # (e.g. "ctd/module1/").  The blobs live at
            # ctd_structure/users/{session_id}/ctd/ctd/module1/.keep so we strip
            # "ctd_structure/users/{session_id}/ctd/" (= user_pfx itself).
            candidates.append((user_pfx, len(user_pfx)))
        candidates.append((f"{_GCS_TEMPLATE}/", len("ctd_structure/ctd/")))

        for prefix, strip_len in candidates:
            paths = []
            for blob in bkt.list_blobs(prefix=prefix):
                if blob.name.endswith("/.keep"):
                    rel    = blob.name[strip_len:]
                    folder = rel[: -len(".keep")]
                    paths.append(folder)
            if paths:
                return sorted(paths)
        return []
    except Exception:
        return []

def _load_canonical_paths(bucket_name: str) -> list[str]:
    """Return only the shared canonical template paths (never a user draft)."""
    return _load_cached_paths(bucket_name, session_id=None)

def _load_clinical_manifest(bucket: str, ta: str, dis: str, drug: str) -> dict | None:
    """Load clinical data manifest.json from GCS for a program. Returns None if absent."""
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_")
    path = f"therapeutic-area/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}/clinical_data/manifest.json"
    try:
        raw = _gcs_client.bucket(bucket).blob(path).download_as_text()
        return json.loads(raw)
    except Exception:
        return None

def _summarise_clinical_manifest(manifest: dict) -> str:
    """Build a Markdown summary of available clinical datasets."""
    sources = manifest.get("sources", [])
    if not sources:
        return "_(manifest found but no datasets registered)_"
    lines = [f"📋 **{len(sources)} clinical dataset(s) registered:**\n"]
    for src in sources:
        cols = src.get("column_mappings", [])
        secs = src.get("ctd_section_keys", [])
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


def _save_to_gcs(bucket_name: str, folder_paths: list[str]) -> None:
    """Write to the shared canonical template (called only on approve)."""
    bkt = _get_bucket(bucket_name)
    if not bkt.exists():
        bkt = _gcs_client.create_bucket(bucket_name)
    scaffold_in_gcs(bkt, _GCS_TEMPLATE, folder_paths)

def _save_draft_to_gcs(bucket_name: str, session_id: str, folder_paths: list[str]) -> None:
    """Write to the user's private draft area (called on extract / refine)."""
    bkt = _get_bucket(bucket_name)
    if not bkt.exists():
        bkt = _gcs_client.create_bucket(bucket_name)
    scaffold_in_gcs(bkt, _gcs_user_ctd_prefix(session_id), folder_paths)

def _save_session_state(bucket_name: str, state: dict) -> None:
    """Persist approved flag + ctd_output to GCS so page-refresh restores them."""
    session_id = (state or {}).get("session_id", "default")
    try:
        bkt  = _get_bucket(bucket_name)
        blob = bkt.blob(_gcs_session_path(session_id))
        payload = {
            "approved":        state.get("approved", False),
            "ctd_output":      state.get("ctd_output"),
            "content_program": state.get("content_program"),
            "content_run_id":  state.get("content_run_id"),
        }
        blob.upload_from_string(json.dumps(payload), content_type="application/json")
    except Exception as exc:
        print(f"[app] Warning: could not save session state: {exc}")

def _load_session_state(bucket_name: str, session_id: str) -> dict:
    """Restore approved flag + ctd_output from GCS (survives page refresh)."""
    try:
        bkt  = _get_bucket(bucket_name)
        blob = bkt.blob(_gcs_session_path(session_id))
        if not blob.exists():
            return {}
        return json.loads(blob.download_as_text())
    except Exception:
        return {}

def _load_extraction_status(bucket_name: str, session_id: str) -> dict:
    """Read the extraction job status written by the worker (from GCS).

    If the status is ``running`` or ``refining`` but ``updated_at`` is older
    than ``_RUNNING_TIMEOUT_SECONDS``, returns ``{"status": "timed_out"}`` so
    the UI can offer a re-extract rather than waiting forever.
    """
    try:
        bkt  = _get_bucket(bucket_name)
        blob = bkt.blob(_gcs_status_path(session_id))
        try:
            job = json.loads(blob.download_as_text())
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
    except Exception:
        return {}

_RUNNING_TIMEOUT_SECONDS = 20 * 60  # 20 minutes — treat older "running" states as timed out


def _check_program_exists(bucket: str, ta: str, dis: str, drug: str) -> bool:
    """Return True if a CTD scaffold already exists for this program in GCS."""
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_")
    prefix = f"{_GCS_PROGRAMS}/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}/ctd/"
    try:
        blobs = list(_gcs_client.bucket(bucket).list_blobs(prefix=prefix, max_results=1))
        return len(blobs) > 0
    except Exception:
        return False


def _write_extraction_status(bucket_name: str, session_id: str, payload: dict) -> None:
    """Write extraction job status to GCS (always stamps updated_at)."""
    try:
        payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
        bkt  = _get_bucket(bucket_name)
        blob = bkt.blob(_gcs_status_path(session_id))
        blob.upload_from_string(json.dumps(payload), content_type="application/json")
    except Exception as exc:
        print(f"[app] Warning: could not write extraction status: {exc}")

def _publish_extraction(bucket: str, ich_url: str, reviewer_email: str | None,
                        session_id: str = "default") -> str:
    """Publish an extraction request to the Pub/Sub topic. Returns the run_id."""
    import uuid
    run_id     = str(uuid.uuid4())
    publisher  = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(_GCP_PROJECT, _PUBSUB_TOPIC)
    payload    = json.dumps({
        "bucket":         bucket,
        "ich_url":        ich_url,
        "reviewer_email": reviewer_email,
        "session_id":     session_id,
        "run_id":         run_id,
    }).encode("utf-8")
    publisher.publish(topic_path, payload).result(timeout=10)
    return run_id

# ── Tree formatter ────────────────────────────────────────────────────────────

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

def _fmt_tree_output(output: CTDStructureOutput, evaluation: EvaluationResult) -> str:
    flagged = {i.path for i in evaluation.issues}
    lines   = []
    for mod in output.modules:
        flag = "  ⚠" if mod.key in flagged else ""
        lines.append(f"{mod.key}/  {mod.label}{flag}")
        for sec in mod.sections:
            flag = "  ⚠" if f"{mod.key}/{sec.key}" in flagged else ""
            lines.append(f"  {sec.key}/  {sec.label}{flag}")
            for sub in sec.subsections:
                lines.append(f"    {sub.key}/  {sub.label}")
        lines.append("")
    n_mod = len(output.modules)
    n_sec = sum(len(m.sections) for m in output.modules)
    n_sub = sum(len(s.subsections) for m in output.modules for s in m.sections)
    lines.append(f"{n_mod} modules · {n_sec} sections · {n_sub} subsections")
    return "\n".join(lines)

# ── Bot init: check GCS on startup ───────────────────────────────────────────

def _initial_state(bucket_name: str, session_id: str) -> tuple[list, dict]:
    """Called once when the page loads to build the opening chatbot message."""
    paths            = _load_cached_paths(bucket_name, session_id)
    canonical_exists = bool(_load_canonical_paths(bucket_name))
    session          = _load_session_state(bucket_name, session_id)
    approved   = session.get("approved", False)
    ctd_output = session.get("ctd_output")

    # If .keep files are missing but session_state.json has a completed ctd_output
    # (e.g. worker was killed after writing the model but before the scaffold finished),
    # reconstruct the paths from the model and re-scaffold so the tree is visible.
    if not paths and ctd_output:
        try:
            output = CTDStructureOutput.model_validate(ctd_output)
            paths  = output.to_folder_paths()
            if paths:
                # Re-write the .keep files and mark status done so future loads skip this.
                _save_draft_to_gcs(bucket_name, session_id, paths)
                _write_extraction_status(bucket_name, session_id, {"status": "done", "n_paths": len(paths)})
        except Exception:
            paths = []

    if paths:
        tree  = _fmt_tree_paths(paths)
        state = {
            "folder_paths":    paths,
            "approved":        approved,
            "ctd_output":      ctd_output,
            "bucket":          bucket_name,
            "session_id":      session_id,
            "canonical_exists": canonical_exists,
        }
        if approved:
            opening = (
                "Welcome back! The **default ICH M4(R4) CTD structure** is loaded and ✅ **published**.\n\n"
                f"```\n{tree}\n```\n\n"
                "Ready to scaffold a program. Tell me the therapeutic area, disease, and drug name — "
                "I'll set up the folder structure and kick off content generation.\n\n"
                "> Example: *set up authoring for oncology / lung cancer / carboplatin*"
            )
        else:
            opening = (
                "I found an existing **ICH M4(R4) CTD folder structure** in GCS.\n\n"
                f"```\n{tree}\n```\n\n"
                "You can:\n"
                "- Say **copy to \u003carea\u003e / \u003cdisease\u003e / \u003cdrug\u003e** to scaffold a program directory\n"
                "- Say **approve** to commit this as the shared default canonical template\n"
                "- Say **re-extract** to rebuild from the ICH index"
            )
    else:
        job = _load_extraction_status(bucket_name, session_id)
        if job.get("status") == "running":
            opening = (
                "⏳ Extraction is still **running in the background**.\n\n"
                "Ask me **status** to check progress, or refresh the page once it's done — "
                "the result will load automatically."
            )
        elif job.get("status") == "refining":
            opening = (
                "⏳ Targeted repair is **running in the background**.\n\n"
                "Ask me **status** to check progress."
            )
        elif job.get("status") == "timed_out":
            age = job.get("age_minutes", "?")
            # Auto-reset so the user doesn't have to manually clear it
            _write_extraction_status(bucket_name, session_id, {"status": "idle"})
            opening = (
                f"⚠️ A background job started {age} minutes ago but never finished "
                "(the worker was likely interrupted by a deployment or timeout).\n\n"
                "Say **extract** to start a fresh extraction."
            )
        else:
            opening = (
                "Hello! I'm your **Regulatory Authoring Assistant**.\n\n"
                "No default CTD structure has been published yet. "
                "Tell me to **build the ICH CTD structure** and I'll query the ICH M4 guidelines "
                "index to assemble the full module → section → subsection hierarchy.\n\n"
                "Once extracted, **approve** it to publish as the default — then scaffold "
                "programs and generate section-level content.\n\n"
                "> ⏳ Structure build takes **3–8 minutes**."
            )
        state = {"folder_paths": [], "approved": False, "canonical_exists": canonical_exists, "bucket": bucket_name, "session_id": session_id}
    return [{"role": "assistant", "content": opening}], state


def _refresh_state_from_gcs(state: dict, bucket: str) -> dict:
    """
    Refresh folder_paths / approved / ctd_output from GCS when Gradio state
    is stale (common after a timer tick updates _state_init but chat() receives
    the old snapshot).  Called once at the top of chat() so every downstream
    function and the coordinator both see fresh data.
    """
    st = state or {}
    bkt = st.get("bucket") or bucket or _DEFAULT_BUCKET
    session_id = st.get("session_id")
    if not session_id:
        return st
    changed = False
    # Always refresh canonical_exists from GCS — the key may already be in state
    # but set to False if canonical was published after this session started.
    _canonical_now = bool(_load_canonical_paths(bkt))
    if _canonical_now != st.get("canonical_exists", False):
        st = {**st, "canonical_exists": _canonical_now}
        changed = True
    if not st.get("folder_paths"):
        fresh = _load_cached_paths(bkt, session_id)
        if fresh:
            st = {**st, "folder_paths": fresh, "bucket": bkt}
            changed = True
    if not st.get("approved") or not st.get("content_program"):
        sess = _load_session_state(bkt, session_id)
        if sess.get("approved") and not st.get("approved"):
            st = {**st, "approved": True}
            changed = True
        if sess.get("ctd_output") and not st.get("ctd_output"):
            st = {**st, "ctd_output": sess["ctd_output"]}
            changed = True
        if sess.get("content_program") and not st.get("content_program"):
            st = {**st, "content_program": sess["content_program"]}
            changed = True
        if sess.get("content_run_id") and not st.get("content_run_id"):
            st = {**st, "content_run_id": sess["content_run_id"]}
            changed = True
    # Cache the GCS scaffold check in state so the coordinator context string
    # is accurate without an extra GCS call in the LLM path.
    prog = st.get("content_program") or {}
    if prog and not st.get("program_scaffold_exists"):
        exists = _check_program_exists(
            bkt,
            prog.get("therapeutic_area", ""),
            prog.get("disease_type", ""),
            prog.get("drug_name", ""),
        )
        if exists:
            st = {**st, "program_scaffold_exists": True}
            changed = True
    return st


# ── Core actions ─────────────────────────────────────────────────────────────

def _do_extract(state: dict, bucket: str, reviewer_email: str,
                history: list, force: bool = False):
    """
    Start extraction.
    - If GCS has a cached structure and force=False → show it immediately.
    - Otherwise start a background thread (connection-independent) and return
      immediately so the SSE connection is not held for 3-8 minutes.
    """
    session_id = (state or {}).get("session_id", "default")
    paths = _load_cached_paths(bucket, session_id)
    if paths and not force:
        session   = _load_session_state(bucket, session_id)
        approved  = session.get("approved", False)
        new_state = {
            **state,
            "folder_paths":      paths,
            "ctd_output":        session.get("ctd_output"),
            "approved":          approved,
            "bucket":            bucket,
            "awaiting_feedback": False,
            "disapproval_feedback": None,
        }
        tree = _fmt_tree_paths(paths)
        reply = (
            "I found a cached structure in GCS — no need to re-query the ICH index.\n\n"
            f"```\n{tree}\n```\n\n"
            "You can copy it to a program now, or say **approve** to commit it as the "
            "shared default canonical template. Say **re-extract** to rebuild from scratch."
        )
        history.append({"role": "assistant", "content": reply})
        yield history, new_state
        return

    # Already running? Only block if the job is genuinely recent (not timed out).
    job = _load_extraction_status(bucket, session_id)
    if job.get("status") == "running":
        history.append({"role": "assistant", "content": (
            "⏳ Extraction is already running in the background.\n\n"
            "Ask me **status** to check progress, or refresh the page once it's done."
        )})
        yield history, state
        return

    # Publish to Pub/Sub — the worker picks it up and runs extraction
    ich_url = (state or {}).get("ich_url") or _DEFAULT_URL
    try:
        run_id = _publish_extraction(bucket, ich_url, reviewer_email or None, session_id)
    except Exception as exc:
        history.append({"role": "assistant",
                        "content": f"❌ Could not queue extraction: {exc}\n\nCheck that the Pub/Sub topic exists and the service account has publish rights."})
        yield history, state
        return

    # Clear any stale disapproval/feedback state so the new extraction starts clean.
    state = {**state, "awaiting_feedback": False, "disapproval_feedback": None,
             "approved": False, "folder_paths": []}

    # Mark status as running immediately so status checks see it before the worker starts.
    # Store run_id so the worker can reject stale duplicate messages.
    _write_extraction_status(bucket, session_id, {"status": "running", "run_id": run_id})

    history.append({"role": "assistant", "content": (
        "🚀 Extraction started in the **background** — querying the ICH index for the full "
        "ICH M4(R4) module → section → subsection hierarchy.\n\n"
        "⏳ This takes **3–8 minutes**. You can:\n"
        "- Ask me **status** at any time to check progress\n"
        "- **Refresh the page** when done — the completed structure will load automatically\n\n"
        
    )})
    yield history, state


def _do_approve(intent: CoordinatorDecision, state: dict, bucket: str,
                reviewer_email: str, history: list):
    """
    Mark the structure as approved and publish it as the shared canonical template.
    """
    folder_paths = (state or {}).get("folder_paths", [])
    if not folder_paths:
        history.append({"role": "assistant",
                        "content": "There's no structure loaded yet. Ask me to **extract** it first."})
        return history, state

    bkt = (state or {}).get("bucket") or bucket or _DEFAULT_BUCKET

    # Promote to shared canonical template — all future users will see this
    try:
        _save_to_gcs(bkt, folder_paths)
    except Exception as exc:
        print(f"[app] Warning: GCS canonical persist on approve: {exc}")
    canonical_note = (
        f"✅ Structure **committed as the shared canonical template** at "
        f"`gs://{bkt}/{_GCS_TEMPLATE}/`.\n\n"
        "All future users will load this as the default structure."
    )

    new_state = {**state, "approved": True, "canonical_exists": True, "bucket": bkt}

    # Persist approved=True to GCS so page-refresh picks it up
    try:
        _save_session_state(bkt, new_state)
    except Exception as exc:
        print(f"[app] Warning: could not save session state on approve: {exc}")

    # If the user already provided the program details in the approve message,
    # chain straight into copy — no extra round-trip needed.
    if intent.therapeutic_area and intent.disease_type and intent.drug_name:
        return _do_copy(intent, new_state, bkt, history)

    history.append({
        "role": "assistant",
        "content": (
            f"{canonical_note}\n\n"
            "To copy the structure into a program directory I need three things:\n\n"
            "| Field | Example |\n"
            "|---|---|\n"
            "| **Therapeutic area** | oncology, neurology, cardiology … |\n"
            "| **Disease / indication** | lung cancer, bells palsy … |\n"
            "| **Drug / compound name** | carboplatin, prednisolone … |\n\n"
            "The structure will be placed at:\n"
            f"`gs://{bkt}/therapeutic-area/<area>/<disease>/<drug>/ctd/`\n\n"
            "> _Example: \"set up for oncology / lung cancer / carboplatin\"_"
        ),
    })
    return history, new_state


def _do_disapprove(intent: CoordinatorDecision, state: dict, history: list):
    """
    User rejected or raised concerns about the shown structure.
    If specific feedback was extracted by the LLM, store it in state and ask
    for confirmation before querying.  Otherwise ask the user what's missing.
    In both cases, set ``awaiting_feedback=True`` so the next user message is
    routed straight to ``_do_refine`` without intent classification.
    """
    feedback = (intent.feedback or "").strip()

    if feedback:
        # LLM already captured specifics — echo them back and wait for any
        # additional detail (or the user can just say "go ahead").
        reply = (
            f"Understood — I caught this concern:\n\n> _{feedback}_\n\n"
            "If that captures it, just say **go ahead** and I'll re-query the "
            "ICH index for those specific areas.  "
            "Or add more detail and I'll include that too."
        )
    else:
        reply = (
            "Got it — can you tell me what looks wrong or incomplete? "
            "For example:\n\n"
            "- *Module 5 sections are missing*\n"
            "- *Subsections under 2.5 Clinical Overview look incomplete*\n"
            "- *I expected more entries in Module 3*\n\n"
            "I'll use your feedback to do a targeted re-query of the ICH index."
        )

    new_state = {
        **state,
        "awaiting_feedback":   True,
        "disapproval_feedback": feedback,   # may be empty; _do_refine will merge
        "approved":            False,
    }
    history.append({"role": "assistant", "content": reply})
    return history, new_state


def _do_refine(feedback: str, state: dict, bucket: str, reviewer_email: str, history: list):
    """
    Called when the user provides (or confirms) disapproval feedback.
    Uses ``refine_from_feedback`` from structure.py to re-query only the
    affected modules/sections and merges the result into the existing structure.
    """
    # Merge any prior captured feedback with what the user just typed
    prior    = (state or {}).get("disapproval_feedback", "").strip()
    combined = " ".join(filter(None, [prior, feedback])).strip() or "general review"

    new_state = {**state, "awaiting_feedback": False, "approved": False}

    # Mark status so the user can ask "status" and see activity
    refine_session_id = new_state.get("session_id", "default")
    refine_bucket = new_state.get("bucket") or bucket or _DEFAULT_BUCKET
    _write_extraction_status(refine_bucket, refine_session_id, {"status": "refining"})

    history.append({"role": "assistant", "content": (
        f"Re-querying the ICH index based on your feedback:\n\n"
        f"> _{combined}_\n\n"
        "⏳ This targets only the affected modules/sections — should be faster than a full extraction."
    )})
    yield history, new_state

    # Reconstruct CTDStructureOutput from the serialised dict stored in state
    raw_output = (state or {}).get("ctd_output")
    if not raw_output:
        # Reset extraction status to idle — do NOT write 'failed' here because
        # that would be shown the next time the user asks for status.
        _write_extraction_status(refine_bucket, refine_session_id, {"status": "idle"})
        history.append({"role": "assistant",
                        "content": "❌ No previous structure found in session. "
                                   "Please run a full **extract** first."})
        yield history, new_state
        return

    try:
        current_output = CTDStructureOutput(**raw_output)
    except Exception as exc:
        _write_extraction_status(refine_bucket, refine_session_id, {"status": "idle"})
        history.append({"role": "assistant",
                        "content": f"❌ Could not deserialise stored structure: {exc}"})
        yield history, new_state
        return

    ich_url = new_state.get("ich_url") or _DEFAULT_URL
    try:
        output, evaluation = refine_from_feedback(ich_url, combined, current_output)
    except Exception as exc:
        _write_extraction_status(refine_bucket, refine_session_id, {"status": "failed", "error": str(exc)})
        history.append({"role": "assistant",
                        "content": f"❌ Refinement query failed: {exc}\n\nTry a full **re-extract**."})
        yield history, new_state
        return

    folder_paths = output.to_folder_paths()
    tree         = _fmt_tree_output(output, evaluation)

    try:
        _save_draft_to_gcs(refine_bucket, refine_session_id, folder_paths)
    except Exception as exc:
        print(f"[app] Warning: GCS persist after refine: {exc}")

    new_state = {
        **new_state,
        "folder_paths": folder_paths,
        "ctd_output":   output.model_dump(),
        "bucket":       refine_bucket,
    }
    try:
        _save_session_state(refine_bucket, new_state)
    except Exception as exc:
        print(f"[app] Warning: could not save session state after refine: {exc}")

    _write_extraction_status(refine_bucket, refine_session_id,
                             {"status": "done", "n_paths": len(folder_paths)})

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
    history[-1] = {"role": "assistant", "content": reply}
    yield history, new_state


def _do_copy(intent: CoordinatorDecision, state: dict, bucket: str, history: list):
    st  = state or {}
    ta   = intent.therapeutic_area.strip().lower().replace(" ", "_")
    dis  = intent.disease_type.strip().lower().replace(" ", "_")
    drug = intent.drug_name.strip().lower().replace(" ", "_")
    bkt  = st.get("bucket") or bucket or _DEFAULT_BUCKET
    gcs_base = f"{_GCS_PROGRAMS}/{ta}/{dis}/{drug}/ctd"

    # Always scaffold from the shared canonical default — never from a user's draft
    canonical_paths = _load_canonical_paths(bkt)
    if not canonical_paths:
        history.append({"role": "assistant", "content": (
            "❌ No default CTD structure has been established yet.\n\n"
            "To set it up:\n"
            "1. Say **extract** to build the ICH M4(R4) hierarchy from the ICH index\n"
            "2. Say **approve** to publish it as the shared default\n\n"
            "Once established, anyone can scaffold programs from it instantly."
        )})
        return history, state

    try:
        bkt_obj = _get_bucket(bkt)
        if not bkt_obj.exists():
            bkt_obj = _gcs_client.create_bucket(bkt)
        scaffold_in_gcs(bkt_obj, gcs_base, canonical_paths)
    except Exception as exc:
        history.append({"role": "assistant",
                        "content": f"❌ GCS copy failed: {exc}"})
        return history, state

    gcs_path = f"gs://{bkt}/{gcs_base}/"
    new_state = {
        **st,
        "program_scaffold_exists": True,
        "canonical_exists": True,
        "content_program": {"therapeutic_area": intent.therapeutic_area.strip(),
                             "disease_type": intent.disease_type.strip(),
                             "drug_name": intent.drug_name.strip()},
    }
    history.append({
        "role": "assistant",
        "content": (
            f"✅ Done! The default CTD structure has been scaffolded at:\n\n"
            f"`{gcs_path}`\n\n"
            f"**{len(canonical_paths)} folder markers** written for "
            f"**{ta.replace('_',' ')} / {dis.replace('_',' ')} / {drug.replace('_',' ')}**.\n\n"
            "---\n"
            "**Before generating content, upload your clinical data:**\n\n"
            "The **🔬 Clinical Data Upload** panel has appeared above. "
            "Upload a clinical trial CSV so the content worker can fill all "
            "`{{placeholder}}` values with real study statistics.\n\n"
            "| Step | Action |\n"
            "|---|---|\n"
            "| 1️⃣ | Open the **🔬 Clinical Data Upload** panel |\n"
            "| 2️⃣ | Upload your clinical trial CSV (fields auto-filled) |\n"
            "| 3️⃣ | Say **generate content** to kick off the 3-pass generation |\n\n"
            "> _Without clinical data, all `{{placeholder}}` values will remain unfilled "
            "and require manual editing._"
        ),
    })
    bkt = (state or {}).get("bucket") or bucket or _DEFAULT_BUCKET
    try:
        _save_session_state(bkt, new_state)
    except Exception as exc:
        print(f"[app] Warning: could not save session state after copy: {exc}")
    return history, new_state


def _do_write(intent: CoordinatorDecision, state: dict, bucket: str, history: list):
    """Publish a content-generation request to the ich4-content-generation Pub/Sub topic."""
    st = state or {}

    ta   = intent.therapeutic_area.strip()
    dis  = intent.disease_type.strip()
    drug = intent.drug_name.strip()
    bkt  = st.get("bucket") or bucket or _DEFAULT_BUCKET
    session_id = st.get("session_id", "default")

    # Auto-scaffold the program directory if it doesn't exist yet.
    if not st.get("program_scaffold_exists"):
        canonical_paths = _load_canonical_paths(bkt)
        if not canonical_paths:
            history.append({"role": "assistant", "content": (
                "❌ No default CTD structure has been established yet.\n\n"
                "Say **extract** to build it, then **approve** to publish it, "
                "and I'll scaffold the program and kick off content generation."
            )})
            return history, state
        try:
            bkt_obj = _get_bucket(bkt)
            scaffolded_base = f"{_GCS_PROGRAMS}/{ta.lower().replace(' ','_')}/{dis.lower().replace(' ','_')}/{drug.lower().replace(' ','_')}/ctd"
            scaffold_in_gcs(bkt_obj, scaffolded_base, canonical_paths)
            st = {**st, "program_scaffold_exists": True}
            history.append({"role": "assistant", "content": (
                f"✅ Program directory scaffolded at `gs://{bkt}/{scaffolded_base}/`. "
                "Now queuing content generation…"
            )})
        except Exception as exc:
            history.append({"role": "assistant", "content": f"❌ Auto-scaffold failed: {exc}"})
            return history, state

    # ── Clinical data check ──────────────────────────────────────────────────
    # Warn the user when no clinical manifest exists — all placeholders would
    # remain as {{key}} [NOT FILLED].  If a manifest is found, summarise it so
    # the user knows exactly what data will be used before committing.
    manifest = _load_clinical_manifest(bkt, ta, dis, drug)
    if not manifest or not manifest.get("sources"):
        # No data — ask for confirmation before proceeding with empty placeholders.
        st_pending = {
            **st,
            "awaiting_write_confirm": {"therapeutic_area": ta, "disease_type": dis, "drug_name": drug},
            "bucket": bkt,
        }
        history.append({"role": "assistant", "content": (
            f"⚠️ **No clinical data found** for **{ta} / {dis} / {drug}**.\n\n"
            "Without clinical data, all `{{placeholder}}` values in the generated "
            "content will appear as `{{placeholder}} [NOT FILLED]` and will need manual editing.\n\n"
            "**Options:**\n"
            "- Say **yes, proceed** to generate content with empty placeholders.\n"
            "- Say **cancel** to stop and upload clinical data first.\n\n"
            "_Tip: register a clinical CSV for this program and the content worker will "
            "automatically fill every placeholder with real study statistics._"
        )})
        return history, st_pending

    # Manifest found — show a brief summary so the user knows what data will be used.
    summary = _summarise_clinical_manifest(manifest)
    history.append({"role": "assistant", "content": (
        f"{summary}\n\n"
        "✅ Content generation will use this real clinical data to fill in all placeholder values. "
        "Queuing now…"
    )})

    try:
        run_id = _publish_content_generation(bkt, ta, dis, drug, session_id)
    except Exception as exc:
        history.append({"role": "assistant", "content": (
            f"❌ Could not queue content generation: {exc}\n\n"
            "Check that the Pub/Sub topic **ich4-content-generation** exists and "
            "the service account has publish rights."
        )})
        return history, state

    new_state = {
        **st,
        "content_run_id": run_id,
        "content_program": {"therapeutic_area": ta, "disease_type": dis, "drug_name": drug},
        "bucket": bkt,
    }
    history.append({"role": "assistant", "content": (
        f"⏳ Content generation queued for "
        f"**{ta} / {dis} / {drug}** (run `{run_id[:8]}…`).\n\n"
        "The worker will:\n"
        "1. Query ICH guidelines (index service)\n"
        "2. Load the clinical datasets listed above\n"
        "3. Generate section templates and fill placeholders with real data\n"
        "4. Validate all CTD sections\n\n"
        "This usually takes 5–10 minutes. Ask me **content status** to check progress, "
        "or I'll notify you automatically when it's done."
    )})
    try:
        _save_session_state(bkt, new_state)
    except Exception as exc:
        print(f"[app] Warning: could not save session state after write: {exc}")
    return history, new_state


# ── Main chat handler ─────────────────────────────────────────────────────────

def chat(
    message: str,
    history: list,
    state: dict,
    ich_url: str,
    bucket: str,
    reviewer_email: str,
):
    history = list(history or [])
    history.append({"role": "user", "content": message})
    bkt = (bucket or "").strip() or _DEFAULT_BUCKET

    # ── Refresh stale Gradio state from GCS once, upfront ───────────────────
    state = _refresh_state_from_gcs(state, bkt)

    # ── Short-circuit: disapproval feedback loop ─────────────────────────────
    # When _do_disapprove sets awaiting_feedback=True, the next user message
    # goes straight to refinement.  Several keyword families break out of the
    # loop so the user can always escape to any positive action.
    _EXTRACT_KEYWORDS = ("re-extract", "reextract", "re extract", "extract", "rebuild", "refresh")
    _ESCAPE_KEYWORDS  = ("generate", "content", "status", "copy", "write", "approve",
                          "scaffold", "set up", "setup")
    _msg_lower = message.strip().lower()
    _is_explicit_extract = any(_msg_lower.startswith(kw) or _msg_lower == kw
                                for kw in _EXTRACT_KEYWORDS)
    _is_escape = any(kw in _msg_lower for kw in _ESCAPE_KEYWORDS)
    if (state or {}).get("awaiting_feedback") and not _is_explicit_extract and not _is_escape:
        gen = _do_refine(message, state, bkt, reviewer_email, history)
        for history, state in gen:
            yield history, state, gr.update(value="")
        return
    # Clear the awaiting_feedback flag when escaping so subsequent messages
    # are routed normally through the coordinator.
    if (state or {}).get("awaiting_feedback") and (_is_explicit_extract or _is_escape):
        state = {**state, "awaiting_feedback": False}

    # ── Short-circuit: write confirmation pending (no clinical data warning) ─
    # When _do_write finds no clinical manifest it sets awaiting_write_confirm
    # and waits for the user to say "yes, proceed" or "cancel".
    if (state or {}).get("awaiting_write_confirm"):
        pending  = state["awaiting_write_confirm"]
        ta_p     = pending.get("therapeutic_area", "")
        dis_p    = pending.get("disease_type", "")
        drug_p   = pending.get("drug_name", "")
        _YES     = ("yes", "proceed", "go ahead", "ok", "sure", "continue", "confirm")
        _NO      = ("no", "cancel", "stop", "skip")
        if any(_msg_lower.startswith(w) for w in _YES):
            st_p = {**state, "awaiting_write_confirm": None}
            try:
                run_id = _publish_content_generation(bkt, ta_p, dis_p, drug_p,
                                                     st_p.get("session_id", "default"))
                new_state = {
                    **st_p,
                    "content_run_id": run_id,
                    "content_program": {"therapeutic_area": ta_p, "disease_type": dis_p, "drug_name": drug_p},
                    "bucket": bkt,
                }
                history.append({"role": "assistant", "content": (
                    f"⏳ Content generation queued for **{ta_p} / {dis_p} / {drug_p}** "
                    f"(run `{run_id[:8]}…`).\n\n"
                    "⚠️ Placeholder values will appear as `{{key}} [NOT FILLED]` — "
                    "edit them manually after generation, or register clinical data and regenerate.\n\n"
                    "Ask me **content status** to check progress."
                )})
                try:
                    _save_session_state(bkt, new_state)
                except Exception as _exc:
                    print(f"[app] Warning: could not save session state after write-confirm: {_exc}")
                yield history, new_state, gr.update(value="")
            except Exception as exc:
                history.append({"role": "assistant",
                                 "content": f"❌ Could not queue content generation: {exc}"})
                yield history, st_p, gr.update(value="")
            return
        elif any(_msg_lower.startswith(w) for w in _NO):
            new_state = {**state, "awaiting_write_confirm": None}
            history.append({"role": "assistant", "content": (
                "OK, content generation cancelled. "
                "Register a clinical CSV for this program first, then say **generate content** again."
            )})
            yield history, new_state, gr.update(value="")
            return
        else:
            # Any other message — clear the flag and fall through to the coordinator.
            state = {**state, "awaiting_write_confirm": None}

    # ── Coordinator: understand + decide in one LangGraph call ───────────────
    # Show a "thinking" placeholder while the two-node graph runs (~1 s).
    history.append({"role": "assistant", "content": "_Thinking…_"})
    yield history, state, gr.update(value="")

    decision: CoordinatorDecision = run_coordinator(message, state)
    history.pop()  # remove the placeholder

    # ── Clarify: coordinator has a question or prerequisite guidance ─────────
    if decision.outcome == "clarify":
        history.append({"role": "assistant", "content": decision.reply or "Could you clarify what you need?"})
        yield history, state, gr.update(value="")
        return

    # ── Proceed: dispatch to the matching action handler ─────────────────────
    intent = decision.intent

    if intent == "extract":
        gen = _do_extract(state, bkt, reviewer_email, history, force=decision.force_reextract)
        for history, state in gen:
            yield history, state, gr.update(value="")

    elif intent == "approve":
        history, state = _do_approve(decision, state, bkt, reviewer_email, history)
        yield history, state, gr.update(value="")

    elif intent == "disapprove":
        history, state = _do_disapprove(decision, state, history)
        yield history, state, gr.update(value="")

    elif intent == "copy":
        history, state = _do_copy(decision, state, bkt, history)
        yield history, state, gr.update(value="")

    elif intent == "write":
        history, state = _do_write(decision, state, bkt, history)
        yield history, state, gr.update(value="")

    elif intent == "rewrite_section":
        # Re-run the full 3-pass pipeline for the programme — same as write.
        # (Targeted single-section re-runs are not yet supported; the 3-pass DAG
        # is idempotent and safe to restart.)
        history, state = _do_write(decision, state, bkt, history)
        yield history, state, gr.update(value="")

    elif intent == "analyze_data":
        ta_a   = (decision.therapeutic_area or "").strip()
        dis_a  = (decision.disease_type or "").strip()
        drug_a = (decision.drug_name or "").strip()
        if ta_a and dis_a and drug_a:
            manifest = _load_clinical_manifest(bkt, ta_a, dis_a, drug_a)
            if manifest and manifest.get("sources"):
                summary = _summarise_clinical_manifest(manifest)
                reply_a = (
                    f"{summary}\n\n"
                    "📊 To run a full statistical analysis, ask me:\n\n"
                    "- *Give me a clinical summary for this program*\n"
                    "- *What is the recovery rate for this study?*\n\n"
                    "Or use the **🔬 Clinical Data Upload** panel to upload or update a CSV."
                )
            else:
                reply_a = (
                    f"⚠️ No clinical data found for **{ta_a} / {dis_a} / {drug_a}**.\n\n"
                    "Use the **🔬 Clinical Data Upload** panel to upload a clinical trial CSV "
                    "and register it against this program first."
                )
        else:
            reply_a = (
                "To analyse clinical data I need to know which program you mean.\n\n"
                "Please tell me the **therapeutic area**, **disease**, and **drug** — "
                "for example: *analyse data for neurology / bells palsy / prednisolone*."
            )
        history.append({"role": "assistant", "content": reply_a})
        yield history, state, gr.update(value="")

    elif intent == "status":
        st       = state or {}
        paths    = st.get("folder_paths", [])
        approved = st.get("approved", False)
        bkt_st     = st.get("bucket") or bkt
        session_id = st.get("session_id", "default")
        job        = _load_extraction_status(bkt_st, session_id)

        # ── Content-generation status (checked first when a run is in progress) ──
        _asking_content = "content" in _msg_lower
        prog = st.get("content_program")
        if prog:
            ta_c, dis_c, drug_c = prog.get("therapeutic_area", ""), prog.get("disease_type", ""), prog.get("drug_name", "")
            cjob   = _load_content_status(bkt_st, ta_c, dis_c, drug_c, session_id)
            cstatus = cjob.get("status")
            if cstatus == "running":
                step       = cjob.get("step", "")
                pass_label = cjob.get("pass_label", "")
                pass_num   = cjob.get("pass_number", "")
                total      = cjob.get("total_passes", 3)
                if pass_label:
                    step_info = f"Pass {pass_num}/{total}: **{pass_label}**"
                elif step:
                    step_info = f"Step: `{step}`"
                else:
                    step_info = "Initialising…"
                history.append({"role": "assistant", "content": (
                    f"⏳ **Content generation is running** — {step_info}\n\n"
                    "**Generation order (ICH M4E(R2) evidence chain):**\n"
                    "1. 📄 Module 5 CSRs → indexed into program RAG namespace\n"
                    "2. 📊 Module 2.7 Clinical Summary → grounded in Module 5 evidence\n"
                    "3. 📝 Module 2.5/2.4/2.3 Overviews → grounded in Module 5 + 2.7\n\n"
                    "⚠️ **Important:** Do not manually regenerate Module 2 without first "
                    "completing Module 5 — the Module 5 index must exist before Module 2 is written. "
                    "Ask me **status** again in a minute to check progress."
                )})
                yield history, state, gr.update(value="")
                return
            elif cstatus == "done":
                history.append({"role": "assistant", "content": (
                    "✅ **Content generation is complete.** "
                    "All three passes (Module 5 → 2.7 → 2.5 overviews) finished successfully.\n\n"
                    "Say **generate content** to regenerate with updated clinical data."
                )})
                yield history, state, gr.update(value="")
                return
            elif cstatus == "failed":
                history.append({"role": "assistant", "content": (
                    f"❌ **Content generation failed** — {cjob.get('error', 'unknown')}\n\n"
                    "Say **generate content** to re-run. The 3-pass DAG is safe to restart — "
                    "Module 5 will be re-indexed automatically before Module 2 is written."
                )})
                yield history, state, gr.update(value="")
                return
            else:
                # prog is set but no active/completed job found
                ta_label   = ta_c.replace("_", " ")
                dis_label  = dis_c.replace("_", " ")
                drug_label = drug_c.replace("_", " ")
                history.append({"role": "assistant", "content": (
                    f"No content generation job has been queued yet for "
                    f"**{ta_label} / {dis_label} / {drug_label}**.\n\n"
                    "Say **generate content** to start the 3-pass pipeline."
                )})
                yield history, state, gr.update(value="")
                return
        elif _asking_content:
            # User asked about content status but no program is set in this session
            history.append({"role": "assistant", "content": (
                "No content program is set up in this session yet.\n\n"
                "To check content status I need to know which program you're working on. "
                "Please scaffold a program first:\n\n"
                "> *Copy the structure to \\<therapeutic area\\> / \\<disease\\> / \\<drug\\>*\n\n"
                "Then say **generate content** to queue the 3-pass pipeline, "
                "and **content status** to monitor it."
            )})
            yield history, state, gr.update(value="")
            return

        # ── Extraction status ─────────────────────────────────────────────────
        if job.get("status") == "refining":
            fresh_paths = _load_cached_paths(bkt_st, session_id)
            if fresh_paths:
                session   = _load_session_state(bkt_st, session_id)
                new_state = {**st, "folder_paths": fresh_paths, "bucket": bkt_st}
                if session.get("ctd_output"):
                    new_state["ctd_output"] = session["ctd_output"]
                _write_extraction_status(bkt_st, session_id, {"status": "done", "n_paths": len(fresh_paths)})
                tree = _fmt_tree_paths(fresh_paths)
                history.append({"role": "assistant", "content": (
                    "✅ **Targeted repair complete!** Here's the updated ICH M4(R4) CTD structure:\n\n"
                    f"```\n{tree}\n```\n\n"
                    "Does this look correct? Say **approve** to accept it, or tell me what else needs fixing."
                )})
                yield history, new_state, gr.update(value="")
            else:
                history.append({"role": "assistant", "content": (
                    "⏳ **Targeted repair is running** — re-querying the ICH index for the affected modules.\n\n"
                    "This is usually faster than a full extraction. Ask me **status** again in a moment."
                )})
                yield history, state, gr.update(value="")

        elif job.get("status") == "running":
            history.append({"role": "assistant", "content": (
                "⏳ **Not yet** — extraction is still running in the background.\n\n"
                "It typically takes 3–8 minutes. Ask me **status** again in a moment to check."
            )})
            yield history, state, gr.update(value="")

        elif job.get("status") == "done":
            fresh_paths = _load_cached_paths(bkt_st, session_id)
            if not fresh_paths:
                session = _load_session_state(bkt_st, session_id)
                raw_output = session.get("ctd_output")
                if raw_output:
                    try:
                        output = CTDStructureOutput.model_validate(raw_output)
                        fresh_paths = output.to_folder_paths()
                        if fresh_paths:
                            _save_draft_to_gcs(bkt_st, session_id, fresh_paths)
                    except Exception:
                        fresh_paths = []
            if fresh_paths:
                tree      = _fmt_tree_paths(fresh_paths)
                session   = _load_session_state(bkt_st, session_id)
                new_state = {**st, "folder_paths": fresh_paths, "bucket": bkt_st}
                if session.get("ctd_output"):
                    new_state["ctd_output"] = session["ctd_output"]
                history.append({"role": "assistant", "content": (
                    "✅ **Yes, extraction is complete!**\n\n"
                    f"```\n{tree}\n```\n\n"
                    "You can copy it to a program now — tell me the therapeutic area, disease, and drug.\n"
                    "Or say **approve** to commit it as the shared default canonical template."
                )})
                yield history, new_state, gr.update(value="")
            else:
                history.append({"role": "assistant", "content": (
                    "✅ Extraction finished, but I couldn't read the paths from GCS — "
                    "try refreshing the page."
                )})
                yield history, state, gr.update(value="")

        elif job.get("status") == "failed":
            history.append({"role": "assistant", "content": (
                f"❌ **Extraction failed** — {job.get('error', 'unknown error')}\n\n"
                "Say **re-extract** to try again."
            )})
            yield history, state, gr.update(value="")

        elif job.get("status") == "timed_out":
            age = job.get("age_minutes", "?")
            history.append({"role": "assistant", "content": (
                f"⚠️ **Job timed out** — the background job started {age} minutes ago "
                "but never finished (likely interrupted by a deployment or Cloud Run timeout).\n\n"
                "Say **re-extract** to start a fresh extraction."
            )})
            _write_extraction_status(bkt_st, session_id, {"status": "idle"})
            yield history, state, gr.update(value="")

        else:
            if not paths:
                paths = _load_cached_paths(bkt_st, session_id)
                if paths:
                    new_state = {**st, "folder_paths": paths, "bucket": bkt_st}
                    session = _load_session_state(bkt_st, session_id)
                    if session.get("ctd_output"):
                        new_state["ctd_output"] = session["ctd_output"]
                    tree = _fmt_tree_paths(paths)
                    history.append({"role": "assistant", "content": (
                        "✅ **Extraction is complete!** Here's the ICH M4(R4) CTD folder structure:\n\n"
                        f"```\n{tree}\n```\n\n"
                        "You can copy it to a program now — tell me the therapeutic area, disease, and drug.\n"
                        "Or say **approve** to commit it as the shared default canonical template."
                    )})
                    yield history, new_state, gr.update(value="")
                    return
            if paths:
                approval_note = (
                    "✅ Already approved." if approved
                    else "Not yet approved — say **approve** when you're happy with it."
                )
                history.append({"role": "assistant", "content": (
                    f"✅ **Structure is loaded** ({len(paths)} folder paths). {approval_note}"
                )})
            else:
                history.append({"role": "assistant", "content": (
                    "No extraction is running and no structure is loaded yet.\n\n"
                    "Say **extract** to build the ICH M4(R4) CTD folder structure from the ICH index."
                )})
            yield history, state, gr.update(value="")

    else:
        history.append({"role": "assistant", "content": (
            "I can help you with the **ICH M4(R4) CTD folder structure**. Here's what you can say:\n\n"
            "- *Generate the CTD structure from the ICH guidelines*\n"
            "- *This looks good, go ahead and approve it*\n"
            "- *Deploy it for the neurology / bells palsy / prednisolone program*\n"
            "- *Generate content for neurology / bells palsy / prednisolone*\n"
            "- *What have you loaded so far?*"
        )})
        yield history, state, gr.update(value="")


# ── Background-job auto-poll ─────────────────────────────────────────────────

def _auto_poll(history: list, state: dict):
    """Called every ~20 s by gr.Timer.

    1. Auto-injects extraction result into chat when the background ctd-worker finishes.
    2. Auto-injects content-generation result when the ich4-content-worker finishes.
    """
    st = state or {}
    bkt = st.get("bucket") or _DEFAULT_BUCKET
    session_id = st.get("session_id")
    if not session_id:
        return history, state

    # ── (A) Extraction poll ──────────────────────────────────────────────────
    if not st.get("folder_paths"):
        job    = _load_extraction_status(bkt, session_id)
        status = job.get("status")
        if status in ("done", "refining"):
            fresh_paths = _load_cached_paths(bkt, session_id)
            if fresh_paths:
                if status == "refining":
                    _write_extraction_status(bkt, session_id, {"status": "done", "n_paths": len(fresh_paths)})
                tree      = _fmt_tree_paths(fresh_paths)
                session   = _load_session_state(bkt, session_id)
                new_state = {**st, "folder_paths": fresh_paths, "bucket": bkt}
                if session.get("ctd_output"):
                    new_state["ctd_output"] = session["ctd_output"]
                label = "Targeted repair complete" if status == "refining" else "Extraction complete"
                new_history = list(history or [])
                new_history.append({"role": "assistant", "content": (
                    f"✅ **{label}!** Here's the ICH M4(R4) CTD folder structure:\n\n"
                    f"```\n{tree}\n```\n\n"
                    "You can copy it to a program now — tell me the therapeutic area, disease, and drug.\n"
                    "Or say **approve** to commit it as the shared default canonical template."
                )})
                return new_history, new_state

    # ── (B) Content-generation poll ──────────────────────────────────────────
    prog = st.get("content_program")
    if prog and st.get("content_run_id"):
        ta, dis, drug = prog.get("therapeutic_area", ""), prog.get("disease_type", ""), prog.get("drug_name", "")
        job    = _load_content_status(bkt, ta, dis, drug, session_id)
        status = job.get("status")
        if status == "done":
            sections  = job.get("sections_written", "?")
            val_pass  = job.get("validation_passed", False)
            val_sum   = job.get("validation_summary", "")
            val_icon  = "✅" if val_pass else "⚠️"
            new_state = {**st, "content_run_id": None}  # clear so we don't re-notify
            new_history = list(history or [])
            new_history.append({"role": "assistant", "content": (
                f"✅ **Content generation complete!**\n\n"
                f"- **{sections} sections** written to GCS\n"
                f"- {val_icon} Validation: {val_sum or ('passed' if val_pass else 'issues found')}\n\n"
                f"Documents saved to `gs://{bkt}/therapeutic-area/"
                f"{ta.replace(' ','_').lower()}/"
                f"{dis.replace(' ','_').lower()}/"
                f"{drug.replace(' ','_').lower()}/ctd/`\n\n"
                "---\n"
                "**📊 How it was generated (ICH M4E(R2) evidence chain)**\n\n"
                "| Pass | Sections | Evidence source |\n"
                "|---|---|---|\n"
                "| 1 — Module 5 CSRs | All 5.3 study reports | Primary data (no prior context) |\n"
                "| 2 — Module 2.7 Clinical Summary | 2.7.3 efficacy, 2.7.4 safety | Indexed Module 5 CSR content |\n"
                "| 3 — Module 2.5/2.4/2.3 Overviews | Clinical & nonclinical overviews | Module 5 + 2.7 indexes |\n\n"
                "⚠️ **If any pass was skipped or the job was interrupted mid-run**, "
                "Module 2 sections may not be grounded in actual CSR findings. "
                "Say **generate content** to re-run the full sequence — it is safe to re-run; "
                "Module 5 will be re-indexed automatically before Module 2 is written.\n\n"
                "**Next steps:**\n"
                "1. 📑 Upload a clinical CSV via the **Clinical Data Upload** panel to register real trial data\n"
                "2. 🔄 Say **generate content** again to re-run with the registered evidence\n"
                "3. 🔍 Review written documents in GCS and fill any remaining `{{placeholder}}` values"
            )})
            return new_history, new_state
        elif status == "failed":
            new_state = {**st, "content_run_id": None}
            new_history = list(history or [])
            step = job.get("step", "")
            # Determine which pass failed to give targeted guidance
            if "module2" in step:
                step_note = (
                    "The job failed during a **Module 2** pass. "
                    "Module 5 CSR content and its index were already created.\n"
                    "Re-running is safe — Module 5 will be re-indexed and Module 2 will "
                    "be rewritten with fresh CSR evidence."
                )
            elif "module5" in step:
                step_note = (
                    "The job failed during the **Module 5 CSR** pass (before indexing). "
                    "No program index exists yet. Re-running starts fresh."
                )
            else:
                step_note = (
                    "Re-run is safe — the full 3-pass sequence "
                    "(Module 5 → 2.7 → 2.5 overviews) will restart from the beginning."
                )
            new_history.append({"role": "assistant", "content": (
                f"❌ **Content generation failed** — {job.get('error', 'unknown error')}\n\n"
                f"{step_note}\n\n"
                "Say **generate content** to try again."
            )})
            return new_history, new_state
        elif status == "timed_out":
            age = job.get("age_minutes", "?")
            new_state = {**st, "content_run_id": None}  # stop polling
            new_history = list(history or [])
            new_history.append({"role": "assistant", "content": (
                f"⚠️ **Content generation timed out** — the job started {age} minutes ago "
                "and never completed (worker was likely interrupted).\n\n"
                "Re-running is safe. The worker will:\n"
                "1. Re-run **Module 5** CSR generation → re-index into program namespace\n"
                "2. Re-run **Module 2.7** Clinical Summary → query Module 5 index for evidence\n"
                "3. Re-run **Module 2.5/2.4/2.3** Overviews → grounded in Module 5 + 2.7\n\n"
                "Say **generate content** to start a fresh run."
            )})
            return new_history, new_state

    return history, state


def _upload_clinical_csv(
    csv_file,
    ta: str,
    dis: str,
    drug: str,
    bucket: str,
) -> str:
    """Gradio handler: register an uploaded CSV for a drug program.

    Returns a Markdown status string shown in the UI.
    """
    if not _CLINICAL_UPLOAD_AVAILABLE:
        return "⚠️ Clinical ingestion package not available in this environment."

    ta   = (ta   or "").strip()
    dis  = (dis  or "").strip()
    drug = (drug or "").strip()

    if not ta or not dis or not drug:
        return "⚠️ Please fill in Therapeutic Area, Disease, and Drug Name before uploading."

    if csv_file is None:
        return "⚠️ No file selected."

    # Gradio 5.x passes the file as a dict with a 'path' key (tmp path on disk)
    local_path = csv_file if isinstance(csv_file, str) else csv_file.get("path", "")
    if not local_path:
        return "⚠️ Could not read uploaded file path."

    bucket = (bucket or "").strip() or _DEFAULT_BUCKET

    try:
        program  = _ProgramInfo(therapeutic_area=ta, disease_type=dis, drug_name=drug)
        api_key  = os.environ.get("OPENAI_API_KEY")
        manifest = _register_csv(
            bucket_name=bucket,
            program=program,
            local_csv_path=local_path,
            api_key=api_key,
        )
        sources = manifest.sources
        src     = next((s for s in sources if s.filename in local_path or True), sources[-1])
        cols    = src.column_mappings if src else []
        secs    = src.ctd_section_keys if src else []
        lines = [
            f"✅ **{src.filename}** registered successfully!\n",
            f"- Study type: **{src.study_type}**",
            f"- Mapped columns: **{len(cols)}**",
            f"- CTD sections: `{'`, `'.join(secs)}`" if secs else "- CTD sections: _(none detected)_",
            f"\nTotal datasets for **{drug}** / **{dis}** / **{ta}**: **{len(sources)}**",
            "\nYou can now say **generate content** in the chat to populate CTD sections with this data.",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"❌ Upload failed: {exc}"


# ── Gradio UI ─────────────────────────────────────────────────────────────────

_CSS = """
footer { display: none !important; }

/* ══════════════════════════════════════════════════════════════
   Force light mode — works regardless of html.dark class
   ══════════════════════════════════════════════════════════════ */

/* Redefine Gradio's own CSS custom properties for both :root and html.dark */
:root,
html.dark {
    --body-background-fill:      #ffffff !important;
    --background-fill-primary:   #ffffff !important;
    --background-fill-secondary: #f5f8fd !important;
    --body-text-color:           #1a1a2e !important;
    --block-background-fill:     #ffffff !important;
    --block-border-color:        #d1dce8 !important;
    --input-background-fill:     #ffffff !important;
    --block-label-text-color:    #1a1a2e !important;
    --block-title-text-color:    #1a1a2e !important;
    --prose-text-color:          #1a1a2e !important;
    --chatbot-background:        #ffffff !important;
}

/* Background */
body,
.gradio-container,
html.dark .gradio-container,
html.dark body {
    background-color: #ffffff !important;
    background: #ffffff !important;
    color: #1a1a2e !important;
}

/* Every text-bearing element */
html.dark .gradio-container,
html.dark .gradio-container p,
html.dark .gradio-container span,
html.dark .gradio-container div,
html.dark .gradio-container label,
html.dark .gradio-container li,
html.dark .gradio-container td,
html.dark .gradio-container th,
html.dark .gradio-container h1,
html.dark .gradio-container h2,
html.dark .gradio-container h3,
html.dark .gradio-container h4 {
    color: #1a1a2e !important;
    background-color: transparent;
}

/* Blocks / panels */
html.dark .block,
html.dark .panel,
html.dark .form,
html.dark fieldset {
    background-color: #ffffff !important;
}

/* Inputs */
html.dark input,
html.dark textarea,
html.dark select {
    background-color: #ffffff !important;
    color: #1a1a2e !important;
}

/* Chatbot bubbles (Gradio 6 markup) */
html.dark .bubble-wrap { background-color: #f5f8fd !important; }
html.dark .prose,
html.dark .prose * { color: #1a1a2e !important; }
html.dark code,
html.dark pre { background-color: #eef3fb !important; color: #1a3a6a !important; }

/* ── Brand header ── */
.rp-header { padding: 16px 0 8px 0; border-bottom: 2px solid #1a4f8a; margin-bottom: 16px; }
.rp-header h1 { font-size: 1.55em; font-weight: 700; color: #1a4f8a !important; margin: 0 0 2px 0; }
.rp-header p  { font-size: 0.88em; color: #444 !important; margin: 0; }

/* ── Capability badges ── */
.rp-badges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.rp-badge {
    font-size: 0.78em; font-weight: 600; padding: 3px 10px;
    border-radius: 12px; background: #eef3fb !important; color: #1a4f8a !important;
    border: 1px solid #b3c9e8;
}

/* ── Chat area ── */
.chatbot-wrap { border-radius: 8px; border: 1px solid #d1dce8 !important; background: #ffffff !important; }

/* ── Quick-action chips ── */
.qa-row { margin-top: 6px; margin-bottom: 2px; }
.qa-row button {
    font-size: 0.80em !important; padding: 4px 12px !important;
    border-radius: 20px !important; font-weight: 500 !important;
    background: #eef3fb !important; color: #1a4f8a !important;
    border: 1px solid #b3c9e8 !important; cursor: pointer;
}
.qa-row button:hover { background: #d6e4f5 !important; }
"""

_EXAMPLES = [
    "Build the ICH M4 CTD structure from guidelines",
    "Approve and publish the default CTD structure",
    "Set up authoring for oncology / lung cancer / carboplatin",
    "Set up authoring for neurology / bells palsy / prednisolone",
    "Generate CTD section content for neurology / bells palsy / prednisolone",
    "What is the current status?",
]


def _update_upload_panel(state: dict):
    """Show/hide and pre-populate the clinical data upload panel.

    Called whenever the session state changes.  When the user has already
    set a drug program via chat, the panel becomes visible and the
    TA/Disease/Drug fields are auto-filled from state['content_program'].
    """
    prog = (state or {}).get("content_program")
    if prog:
        return (
            gr.update(visible=True),
            gr.update(value=prog.get("therapeutic_area", "")),
            gr.update(value=prog.get("disease_type", "")),
            gr.update(value=prog.get("drug_name", "")),
        )
    return (
        gr.update(visible=True),
        gr.update(value=""),
        gr.update(value=""),
        gr.update(value=""),
    )

_HEAD = """
<script>
(function() {
    var html = document.documentElement;
    html.classList.remove('dark');
    html.style.colorScheme = 'light';
    new MutationObserver(function() {
        if (html.classList.contains('dark')) {
            html.classList.remove('dark');
            html.style.colorScheme = 'light';
        }
    }).observe(html, { attributes: true, attributeFilter: ['class'] });
})();
</script>
"""

_theme = gr.themes.Soft().set(
    body_background_fill="white",
    body_background_fill_dark="white",
    body_text_color="#1a1a2e",
    body_text_color_dark="#1a1a2e",
    body_text_color_subdued="#444444",
    body_text_color_subdued_dark="#444444",
    background_fill_primary="white",
    background_fill_primary_dark="white",
    background_fill_secondary="#f5f8fd",
    background_fill_secondary_dark="#f5f8fd",
    block_background_fill="white",
    block_background_fill_dark="white",
    block_label_text_color="#1a1a2e",
    block_label_text_color_dark="#1a1a2e",
    block_title_text_color="#1a1a2e",
    block_title_text_color_dark="#1a1a2e",
    input_background_fill="white",
    input_background_fill_dark="white",
    input_placeholder_color="#888888",
    input_placeholder_color_dark="#888888",
    panel_background_fill="white",
    panel_background_fill_dark="white",
    code_background_fill="#eef3fb",
    code_background_fill_dark="#eef3fb",
)

with gr.Blocks(title="Regulatory Authoring Platform") as demo:

    with gr.Column(elem_classes=["rp-header"]):
        gr.HTML(
            "<h1>&#9878; Regulatory Authoring Platform</h1>"
            "<p>AI-assisted authoring for ICH M4(R4) Common Technical Documents &mdash; "
            "from structure setup to section-level content generation.</p>"
        )

    with gr.Row(elem_classes=["rp-badges"]):
        for _badge in [
            "📋 ICH M4 CTD Structure",
            "📝 Section Content Generation",
            "🔬 Clinical Data Integration",
            "✅ Regulatory Validation",
            "☁️ GCS Document Store",
        ]:
            gr.HTML(f'<span class="rp-badge">{_badge}</span>')

    _state_init     = gr.State(value={})
    _browser_session = gr.BrowserState("")  # UUID persisted in localStorage per browser

    chatbot = gr.Chatbot(
        value=[],
        label="",
        elem_classes=["chatbot-wrap"],
        height=500,
        show_label=False,
        avatar_images=(None, None),
        render_markdown=True,
    )

    msg_input = gr.Textbox(
        placeholder="Describe what you need — e.g. set up authoring for oncology / lung cancer / carboplatin",
        label="",
        lines=1,
        max_lines=4,
        autofocus=True,
        show_label=False,
    )

    with gr.Row(elem_classes=["qa-row"]):
        for ex in _EXAMPLES:
            gr.Button(ex, size="sm").click(
                fn=lambda m=ex: m,
                outputs=msg_input,
            )

    with gr.Accordion("⚙  Configuration", open=False):
        with gr.Row():
            ich_url_input = gr.Textbox(label="ICH Guidelines Index URL", value=_DEFAULT_URL,    scale=3)
            email_input   = gr.Textbox(label="Compliance officer email",  placeholder="officer@example.com", scale=2)
            bucket_input  = gr.Textbox(label="GCS document bucket",       value=_DEFAULT_BUCKET, scale=2)

    with gr.Accordion("🔬  Clinical Data Upload", open=False, visible=True) as _upload_accordion:
        gr.Markdown(
            "Upload a clinical trial CSV for the currently active drug program. "
            "The program must be configured via chat before uploading."
        )
        with gr.Row():
            upload_ta   = gr.Textbox(label="Therapeutic Area",  placeholder="e.g. neurology",    scale=1)
            upload_dis  = gr.Textbox(label="Disease",           placeholder="e.g. bells palsy",  scale=1)
            upload_drug = gr.Textbox(label="Drug Name",         placeholder="e.g. prednisolone", scale=1)
        with gr.Row():
            upload_file = gr.File(
                label="Clinical CSV file",
                file_types=[".csv"],
                scale=3,
            )
            upload_btn  = gr.Button("Upload & Register", variant="primary", scale=1)
        upload_status = gr.Markdown(value="", label="")

        upload_btn.click(
            fn=_upload_clinical_csv,
            inputs=[upload_file, upload_ta, upload_dis, upload_drug, bucket_input],
            outputs=upload_status,
        )

    def _on_load(bucket: str, browser_session: str):
        sid = browser_session or str(uuid.uuid4())
        history, state = _initial_state(bucket or _DEFAULT_BUCKET, sid)
        return history, state, sid

    demo.load(fn=_on_load, inputs=[bucket_input, _browser_session],
              outputs=[chatbot, _state_init, _browser_session])

    _state_init.change(
        fn=_update_upload_panel,
        inputs=[_state_init],
        outputs=[_upload_accordion, upload_ta, upload_dis, upload_drug],
    )

    _inputs  = [msg_input, chatbot, _state_init, ich_url_input, bucket_input, email_input]
    _outputs = [chatbot, _state_init, msg_input]

    msg_input.submit(chat, _inputs, _outputs)

    # Auto-poll: check GCS every 20 s and surface the result as soon as the
    # background worker finishes — no need for the user to type "status".
    poll_timer = gr.Timer(value=20, active=True)
    poll_timer.tick(fn=_auto_poll,
                    inputs=[chatbot, _state_init],
                    outputs=[chatbot, _state_init])


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    demo.launch(server_name="0.0.0.0", server_port=port,
                css=_CSS, theme=_theme, head=_HEAD)
