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
from ctd_structure.structure import (                                    # noqa: E402
    CTDStructureOutput,
    EvaluationResult,
    refine_from_feedback,
)
from main import CoordinatorDecision, IntentResult, parse_intent, run_coordinator  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_URL    = os.environ.get("ICH_INDEX_URL",
                                 "https://ich4-index-811317821863.us-central1.run.app")
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

def _content_status_path(ta: str, dis: str, drug: str, session_id: str) -> str:
    def _slug(s: str) -> str:
        return s.strip().lower().replace(" ", "_")
    return (
        f"{_GCS_PROGRAMS}/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}"
        f"/content_status/{session_id}.json"
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
    """Read content-generation job status from GCS. Returns {} if not found."""
    try:
        path = _content_status_path(ta, dis, drug, session_id)
        raw  = _gcs_client.bucket(bucket).blob(path).download_as_text()
        job  = json.loads(raw)
    except Exception:
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
            "approved":   state.get("approved", False),
            "ctd_output": state.get("ctd_output"),
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
    paths   = _load_cached_paths(bucket_name, session_id)
    session = _load_session_state(bucket_name, session_id)
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
            "folder_paths": paths,
            "approved":     approved,
            "ctd_output":   ctd_output,
            "bucket":       bucket_name,
            "session_id":   session_id,
        }
        if approved:
            opening = (
                "Welcome back! The **ICH M4(R4) CTD folder structure** is loaded and ✅ **already approved**.\n\n"
                f"```\n{tree}\n```\n\n"
                "Ready to copy to a program directory. Tell me the therapeutic area, disease, and drug name.\n\n"
                "> Example: *set up for oncology / lung cancer / carboplatin*"
            )
        else:
            opening = (
                "I found an existing **ICH M4(R4) CTD folder structure** in GCS.\n\n"
                f"```\n{tree}\n```\n\n"
                "Does this look correct? Say **yes / approve** to accept it and unlock "
                "copying to a program directory, or tell me to **re-extract** to rebuild "
                "from the ICH index."
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
                "Hello! I'm your **ICH CTD Structure assistant**.\n\n"
                "No existing structure was found in GCS. "
                "Tell me to **extract** (or *generate / build*) the canonical "
                "ICH M4(R4) CTD folder hierarchy — I'll query the ICH index and "
                "present the structure for your review before saving anything.\n\n"
                "> ⏳ Extraction takes **3–8 minutes**."
            )
        state = {"folder_paths": [], "approved": False, "bucket": bucket_name, "session_id": session_id}
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
    if not st.get("folder_paths"):
        fresh = _load_cached_paths(bkt, session_id)
        if fresh:
            st = {**st, "folder_paths": fresh, "bucket": bkt}
            changed = True
    if not st.get("approved"):
        sess = _load_session_state(bkt, session_id)
        if sess.get("approved"):
            st = {**st, "approved": True}
            changed = True
        if sess.get("ctd_output") and not st.get("ctd_output"):
            st = {**st, "ctd_output": sess["ctd_output"]}
            changed = True
    # Cache the GCS scaffold check in state so the coordinator context string
    # is accurate without an extra GCS call in the LLM path.
    prog = st.get("content_program") or {}
    if prog and not st.get("program_scaffold_exists"):
        exists = _check_program_exists(
            bkt,
            prog.get("ta", ""),
            prog.get("dis", ""),
            prog.get("drug", ""),
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
            "Say **approve** if this looks good, or ask me to **re-extract** to rebuild it."
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
        "_Your connection won't be interrupted while it runs._"
    )})
    yield history, state


def _do_approve(intent: CoordinatorDecision, state: dict, bucket: str,
                reviewer_email: str, history: list):
    """
    Mark the structure as approved.
    - Any user: saves to their own draft + marks session approved.
    - Compliance officer only: also promotes to the shared canonical template.
    """
    folder_paths = (state or {}).get("folder_paths", [])
    if not folder_paths:
        history.append({"role": "assistant",
                        "content": "There's no structure loaded yet. Ask me to **extract** it first."})
        return history, state

    bkt = (state or {}).get("bucket") or bucket or _DEFAULT_BUCKET
    is_compliance = (reviewer_email or "").strip().lower() in _COMPLIANCE_OFFICERS

    if is_compliance:
        # Promote to shared canonical template — all future users will see this
        try:
            _save_to_gcs(bkt, folder_paths)
        except Exception as exc:
            print(f"[app] Warning: GCS canonical persist on approve: {exc}")
        canonical_note = (
            f"✅ Structure **promoted to the shared canonical template** at "
            f"`gs://{bkt}/{_GCS_TEMPLATE}/`.\n\n"
            "All future users will load this as the default structure."
        )
    else:
        canonical_note = (
            "⚠️ Your approval has been recorded, but the **shared canonical template "
            "will only be updated by a compliance officer**.\n\n"
            "A compliance officer can approve the same structure to publish it for everyone."
        )

    new_state = {**state, "approved": True, "bucket": bkt}

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
        _write_extraction_status(refine_bucket, refine_session_id, {"status": "failed", "error": "no prior structure in session"})
        history.append({"role": "assistant",
                        "content": "❌ No previous structure found in session. "
                                   "Please run a full **extract** first."})
        yield history, new_state
        return

    try:
        current_output = CTDStructureOutput(**raw_output)
    except Exception as exc:
        _write_extraction_status(refine_bucket, refine_session_id, {"status": "failed", "error": str(exc)})
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
        "Does the updated structure look correct? "
        "Say **approve** to accept, or tell me what else needs fixing."
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

    try:
        bkt_obj = _get_bucket(bkt)
        if not bkt_obj.exists():
            bkt_obj = _gcs_client.create_bucket(bkt)
        scaffold_in_gcs(bkt_obj, gcs_base, st["folder_paths"])
    except Exception as exc:
        history.append({"role": "assistant",
                        "content": f"❌ GCS copy failed: {exc}"})
        return history, state

    gcs_path = f"gs://{bkt}/{gcs_base}/"
    # Mark the scaffold as existing in state so subsequent write calls know
    new_state = {
        **st,
        "program_scaffold_exists": True,
        "content_program": {"ta": intent.therapeutic_area.strip(),
                             "dis": intent.disease_type.strip(),
                             "drug": intent.drug_name.strip()},
    }
    history.append({
        "role": "assistant",
        "content": (
            f"✅ Done! The CTD folder structure has been scaffolded at:\n\n"
            f"`{gcs_path}`\n\n"
            f"**{len(st['folder_paths'])} folder markers** written for "
            f"**{ta.replace('_',' ')} / {dis.replace('_',' ')} / {drug.replace('_',' ')}**.\n\n"
            "Ready to generate content? Say **generate content** and I'll kick it off."
        ),
    })
    return history, new_state


def _do_write(intent: CoordinatorDecision, state: dict, bucket: str, history: list):
    """Publish a content-generation request to the ich4-content-generation Pub/Sub topic."""
    st = state or {}

    ta   = intent.therapeutic_area.strip()
    dis  = intent.disease_type.strip()
    drug = intent.drug_name.strip()
    bkt  = st.get("bucket") or bucket or _DEFAULT_BUCKET
    session_id = st.get("session_id", "default")

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
        "content_program": {"ta": ta, "dis": dis, "drug": drug},
        "bucket": bkt,
    }
    history.append({"role": "assistant", "content": (
        f"⏳ Content generation queued for "
        f"**{ta} / {dis} / {drug}** (run `{run_id[:8]}…`).\n\n"
        "The worker will:\n"
        "1. Query ICH guidelines (index service)\n"
        "2. Generate section templates\n"
        "3. Write and validate all CTD sections\n\n"
        "This usually takes 5–10 minutes. Ask me **content status** to check progress, "
        "or I'll notify you automatically when it's done."
    )})
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
    # goes straight to refinement.  Explicit extract keywords break out of the
    # loop so the user can always escape with a fresh extraction.
    _EXTRACT_KEYWORDS = ("re-extract", "reextract", "re extract", "extract", "rebuild", "refresh")
    _msg_lower = message.strip().lower()
    _is_explicit_extract = any(_msg_lower.startswith(kw) or _msg_lower == kw
                                for kw in _EXTRACT_KEYWORDS)
    if (state or {}).get("awaiting_feedback") and not _is_explicit_extract:
        gen = _do_refine(message, state, bkt, reviewer_email, history)
        for history, state in gen:
            yield history, state, gr.update(value="")
        return

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

    elif intent == "status":
        st       = state or {}
        paths    = st.get("folder_paths", [])
        approved = st.get("approved", False)
        bkt_st     = st.get("bucket") or bkt
        session_id = st.get("session_id", "default")
        job        = _load_extraction_status(bkt_st, session_id)

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
                    "Does this look correct? Say **approve** to accept it, or **re-extract** if something looks off."
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
                        "Does this look correct? Say **approve** to accept it, or "
                        "**re-extract** if something looks off."
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
                    "Does this look correct? Say **approve** to accept it, or "
                    "**re-extract** if something looks off."
                )})
                return new_history, new_state

    # ── (B) Content-generation poll ──────────────────────────────────────────
    prog = st.get("content_program")
    if prog and st.get("content_run_id"):
        ta, dis, drug = prog.get("ta", ""), prog.get("dis", ""), prog.get("drug", "")
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
                f"{drug.replace(' ','_').lower()}/ctd/`"
            )})
            return new_history, new_state
        elif status == "failed":
            new_state = {**st, "content_run_id": None}
            new_history = list(history or [])
            new_history.append({"role": "assistant", "content": (
                f"❌ **Content generation failed** — {job.get('error', 'unknown error')}\n\n"
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
                "Say **generate content** to start a fresh run."
            )})
            return new_history, new_state

    return history, state

# ── Gradio UI ─────────────────────────────────────────────────────────────────

_CSS = """
footer { display: none !important; }
.chatbot-wrap { border-radius: 8px; }
.examples-row { margin-top: 4px; }
.examples-row button {
    font-size: 0.82em !important;
    padding: 4px 10px !important;
    border-radius: 20px !important;
    background: var(--background-fill-secondary) !important;
    border: 1px solid var(--border-color-primary) !important;
    cursor: pointer;
}
"""

_EXAMPLES = [
    "Extract the ICH CTD structure from the index",
    "Approve the structure",
    "Set up CTD structure for oncology / lung cancer / carboplatin",
    "Set up CTD structure for neurology / bells palsy / prednisolone",
    "What is the current status?",
    "Re-extract fresh from the ICH index",
]

with gr.Blocks(title="ICH CTD Structure Assistant", css=_CSS, theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        "# ICH CTD Structure Assistant\n"
        "Describe what you need in plain English — I'll handle the rest."
    )

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
        placeholder="Type your message and press Enter…",
        label="",
        lines=1,
        max_lines=4,
        autofocus=True,
        show_label=False,
    )

    with gr.Row(elem_classes=["examples-row"]):
        for ex in _EXAMPLES:
            gr.Button(ex, size="sm").click(
                fn=lambda m=ex: m,
                outputs=msg_input,
            )

    with gr.Accordion("⚙  Settings", open=False):
        with gr.Row():
            ich_url_input = gr.Textbox(label="ICH Index URL",             value=_DEFAULT_URL,    scale=3)
            email_input   = gr.Textbox(label="Reviewer email (optional)", placeholder="reviewer@example.com", scale=2)
            bucket_input  = gr.Textbox(label="GCS bucket",                value=_DEFAULT_BUCKET, scale=2)

    def _on_load(bucket: str, browser_session: str):
        sid = browser_session or str(uuid.uuid4())
        history, state = _initial_state(bucket or _DEFAULT_BUCKET, sid)
        return history, state, sid

    demo.load(fn=_on_load, inputs=[bucket_input, _browser_session],
              outputs=[chatbot, _state_init, _browser_session])

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
    demo.launch(server_name="0.0.0.0", server_port=port)
