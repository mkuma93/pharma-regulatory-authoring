"""
ui/app.py

Top-level Gradio UI — the user-facing gateway for the Regulatory Authoring Platform.

Responsibilities
────────────────
  • Render the chat interface (no business logic, no GCS, no Pub/Sub)
  • Run the coordinator (LangGraph: understand intent + check prerequisites)
  • Route each intent to the correct downstream API:
      CTD intents  → ctd-api  (structure, scaffold, content generation)
      (future)     → clinical-api, validation-api, …
  • Auto-poll background job status
  • Handle file uploads via ctd-api

Routing table
─────────────
  intent == "extract"    → POST ctd-api/extract
  intent == "approve"    → POST ctd-api/approve
  intent == "disapprove" → POST ctd-api/disapprove
  intent == "copy"       → POST ctd-api/copy
  intent == "write"      → POST clinical-analyst/trigger
  intent == "status"     → GET  ctd-api/status_query
  clarify / help         → handled directly (coordinator already has the reply)

Environment variables
─────────────────────
  CTD_API_URL    — base URL of the ctd-api Cloud Run service
  GCS_BUCKET     — default bucket (passed through to APIs)
  PORT           — port to bind (injected by Cloud Run)
  OPENAI_API_KEY — used by the coordinator LLM
"""
from __future__ import annotations

import os
import sys
import uuid

import gradio as gr
import requests

# ── Path bootstrap ────────────────────────────────────────────────────────────
# main.py (coordinator) lives alongside this file in the ui/ directory.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from coordinator import CoordinatorDecision, run_coordinator  # global UI coordinator

# ── Config ────────────────────────────────────────────────────────────────────

_CTD_API_URL             = os.environ.get("CTD_API_URL", "http://localhost:8081")
_ICH4_WRITER_URL         = os.environ.get("ICH4_WRITER_URL", "http://localhost:8083")
_CLINICAL_ANALYST_URL    = os.environ.get("CLINICAL_ANALYST_URL", "http://localhost:8084")
_DEFAULT_BUCKET          = os.environ.get("GCS_BUCKET", "pharma-reguatory-author-life-science")

# ── OIDC helper ───────────────────────────────────────────────────────────────

def _oidc_headers(base_url: str) -> dict[str, str]:
    """Return Authorization header with a service-account OIDC token.

    Audience is the base URL of the target Cloud Run service (no path).
    Falls back to empty dict (unauthenticated) when running locally without
    ADC/metadata server — prevents dev-loop breakage.
    """
    try:
        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2.id_token import fetch_id_token
        # Strip path — Cloud Run audience is always the service root URL
        from urllib.parse import urlparse
        p = urlparse(base_url)
        audience = f"{p.scheme}://{p.netloc}"
        token = fetch_id_token(AuthRequest(), audience)
        return {"Authorization": f"Bearer {token}"}
    except Exception:
        return {}

# ── API client helpers ────────────────────────────────────────────────────────

def _ctd_post(path: str, payload: dict, timeout: int = 60) -> dict:
    """POST to ctd-api and return parsed JSON. Returns error reply on failure."""
    url = f"{_CTD_API_URL.rstrip('/')}{path}"
    try:
        resp = requests.post(url, json=payload, headers=_oidc_headers(_CTD_API_URL), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"reply": f"❌ CTD API error (`{path}`): {exc}", "state_patch": {}}


def _ctd_get(path: str, params: dict, timeout: int = 30) -> dict:
    """GET from ctd-api and return parsed JSON. Returns {} on failure."""
    url = f"{_CTD_API_URL.rstrip('/')}{path}"
    try:
        resp = requests.get(url, params={k: v for k, v in params.items() if v is not None},
                            headers=_oidc_headers(_CTD_API_URL), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}


def _writer_get(path: str, params: dict, timeout: int = 30) -> dict:
    """GET from ich4-writer and return parsed JSON. Returns {} on failure."""
    url = f"{_ICH4_WRITER_URL.rstrip('/')}{path}"
    try:
        resp = requests.get(url, params={k: v for k, v in params.items() if v is not None},
                            headers=_oidc_headers(_ICH4_WRITER_URL), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}


def _ich4_post(base_url: str, path: str, payload: dict, timeout: int = 300) -> dict:
    """POST to an ICH4 service (orchestrator or writer). Returns error reply on failure."""
    url = f"{base_url.rstrip('/')}{path}"
    try:
        resp = requests.post(url, json=payload, headers=_oidc_headers(base_url), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"reply": f"❌ ICH4 service error (`{path}`): {exc}", "state_patch": {}}


def _analyst_post(path: str, payload: dict, timeout: int = 120) -> dict:
    """POST to the clinical-analyst service. Returns error reply on failure."""
    url = f"{_CLINICAL_ANALYST_URL.rstrip('/')}{path}"
    try:
        resp = requests.post(url, json=payload, headers=_oidc_headers(_CLINICAL_ANALYST_URL), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"reply": f"❌ Clinical analyst error (`{path}`): {exc}", "state_patch": {}}


# ── UI helper ─────────────────────────────────────────────────────────────────

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


# ── Page load: hydrate session state from ctd-api ────────────────────────────

def _initial_state(bucket: str, browser_session: str):
    """Called once when the page loads."""
    sid  = browser_session or str(uuid.uuid4())
    bkt  = (bucket or "").strip() or _DEFAULT_BUCKET
    data = _ctd_get("/session", {"session_id": sid, "bucket": bkt})

    paths    = data.get("folder_paths", [])
    approved = data.get("approved", False)
    state = {
        "session_id":               sid,
        "bucket":                   bkt,
        "folder_paths":             paths,
        "approved":                 approved,
        "canonical_exists":         data.get("canonical_exists", False),
        "program_scaffold_exists":  data.get("program_scaffold_exists", False),
        "ctd_output":               data.get("ctd_output"),
        "content_program":          data.get("content_program"),
        "content_run_id":           data.get("content_run_id"),
    }

    if paths:
        tree = _fmt_tree_paths(paths)
        if approved:
            opening = (
                "Welcome back! The **default ICH M4(R4) CTD structure** is loaded and ✅ **published**.\n\n"
                f"```\n{tree}\n```\n\n"
                "Ready to scaffold a program. Tell me the therapeutic area, disease, and drug name.\n\n"
                "> Example: *set up authoring for oncology / lung cancer / carboplatin*"
            )
        else:
            opening = (
                "I found an existing **ICH M4(R4) CTD folder structure** in GCS.\n\n"
                f"```\n{tree}\n```\n\n"
                "- Say **copy to <area> / <disease> / <drug>** to scaffold a program directory\n"
                "- Say **approve** to commit this as the shared default canonical template\n"
                "- Say **re-extract** to rebuild from the ICH index"
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

    return [{"role": "assistant", "content": opening}], state, sid


# ── Main chat handler ─────────────────────────────────────────────────────────

def chat(message: str, history: list, state: dict, bucket: str, reviewer_email: str):
    """
    1. Handle short-circuit state machine (awaiting_feedback, awaiting_write_confirm).
    2. Run coordinator → CoordinatorDecision.
    3. If clarify/help → reply directly (no API call).
    4. If proceed → route to the correct API endpoint.

    Returns a single tuple (no streaming/SSE) so IAP proxying works correctly.
    """
    history    = list(history or [])
    state      = state or {}
    bkt        = (bucket or "").strip() or _DEFAULT_BUCKET
    msg_lower  = message.strip().lower()
    session_id = state.get("session_id", "default")

    history.append({"role": "user", "content": message})

    # ── Short-circuit: disapproval feedback loop ──────────────────────────────
    _EXTRACT_KW = ("re-extract", "reextract", "re extract", "extract", "rebuild", "refresh")
    _ESCAPE_KW  = ("generate", "content", "status", "copy", "write", "approve",
                   "scaffold", "set up", "setup", "template", "analyse", "analyze",
                   "rewrite", "section", "data")
    is_extract = any(msg_lower.startswith(kw) or msg_lower == kw for kw in _EXTRACT_KW)
    is_escape  = any(kw in msg_lower for kw in _ESCAPE_KW)

    if state.get("awaiting_feedback") and not is_extract and not is_escape:
        result = _ctd_post("/refine", {
            "session_id":     session_id,
            "bucket":         bkt,
            "feedback":       message,
            "prior_feedback": state.get("disapproval_feedback", "") or "",
            "ctd_output":     state.get("ctd_output"),
        })
        history.append({"role": "assistant", "content": result.get("reply", "_(no reply)_")})
        state = {**state, **result.get("state_patch", {})}
        return history, state, gr.update(value="")

    if state.get("awaiting_feedback") and (is_extract or is_escape):
        state = {**state, "awaiting_feedback": False}

    # ── Short-circuit: awaiting write confirmation (no clinical data) ─────────
    if state.get("awaiting_write_confirm"):
        pending = state["awaiting_write_confirm"]
        ta_p, dis_p, drug_p = pending.get("therapeutic_area", ""), pending.get("disease_type", ""), pending.get("drug_name", "")
        _YES = ("yes", "proceed", "go ahead", "ok", "sure", "continue", "confirm")
        _NO  = ("no", "cancel", "stop", "skip")

        if any(msg_lower.startswith(w) for w in _YES):
            result = _analyst_post("/trigger", {
                "session_id":        session_id,
                "bucket":            bkt,
                "therapeutic_area":  ta_p,
                "disease_type":      dis_p,
                "drug_name":         drug_p,
                "force_no_clinical": True,
                "state":             state,
            })
            history.append({"role": "assistant", "content": result.get("reply", "_(no reply)_")})
            state = {**state, **result.get("state_patch", {})}
            return history, state, gr.update(value="")

        elif any(msg_lower.startswith(w) for w in _NO):
            history.append({"role": "assistant", "content": (
                "OK, content generation cancelled. "
                "Register a clinical CSV for this program first, then say **generate content** again."
            )})
            state = {**state, "awaiting_write_confirm": None}
            return history, state, gr.update(value="")
        else:
            state = {**state, "awaiting_write_confirm": None}

    # ── Coordinator: understand intent + check prerequisites ──────────────────
    decision: CoordinatorDecision = run_coordinator(message, state)

    # ── Clarify: coordinator composes the reply — no downstream API call ──────
    if decision.outcome == "clarify":
        history.append({"role": "assistant", "content": decision.reply or "Could you clarify what you need?"})
        return history, state, gr.update(value="")

    # ── Workflow pre-flight helper ─────────────────────────────────────────────
    # Reusable state checks used across multiple intents.
    _canonical_ready   = state.get("approved") or state.get("canonical_exists")
    _prog_state        = state.get("content_program") or state.get("ctd_program") or {}
    _scaffold_ready    = state.get("program_scaffold_exists") or bool(_prog_state)

    def _guide(msg: str):
        """Append a guidance reply and return early from chat()."""
        history.append({"role": "assistant", "content": msg})
        return history, {**state}, gr.update(value="")

    # ── Proceed: route to the correct API ────────────────────────────────────
    intent = decision.intent
    result: dict = {}

    if intent == "extract":
        # No pre-requisite — this is always step 1
        result = _ctd_post("/extract", {
            "session_id":     session_id,
            "bucket":         bkt,
            "reviewer_email": reviewer_email or None,
            "force":          decision.force_reextract,
        })

    elif intent == "approve":
        # Prerequisite: a CTD structure must have been extracted (folder_paths exist)
        if not state.get("folder_paths"):
            return _guide(
                "⚠️ **Nothing to approve yet.**\n\n"
                "The workflow starts by building the ICH CTD structure:\n\n"
                "**Step 1** → Say **build the ICH CTD structure** — I'll query the ICH M4 guidelines "
                "index and assemble the module → section → subsection hierarchy.\n\n"
                "Once extracted you can review it and say **approve** to publish it as the default template.\n\n"
                "> ⏳ Structure build takes 3–8 minutes."
            )
        result = _ctd_post("/approve", {
            "session_id":       session_id,
            "bucket":           bkt,
            "folder_paths":     state.get("folder_paths", []),
            "therapeutic_area": decision.therapeutic_area,
            "disease_type":     decision.disease_type,
            "drug_name":        decision.drug_name,
            "state":            state,
        })

    elif intent == "disapprove":
        # Prerequisite: a CTD structure must exist to disapprove
        if not state.get("folder_paths") and not _canonical_ready:
            return _guide(
                "⚠️ **No CTD structure to reject.**\n\n"
                "Say **build the ICH CTD structure** first to extract a structure you can review."
            )
        result = _ctd_post("/disapprove", {
            "session_id": session_id,
            "feedback":   decision.feedback,
        })

    elif intent == "copy":
        # Prerequisite 1: canonical template must be approved
        if not _canonical_ready:
            return _guide(
                "⚠️ **No default CTD template approved yet.**\n\n"
                "Before setting up a program folder, publish the default template:\n\n"
                "1. Say **build the ICH CTD structure** — extracts the full module hierarchy (3–8 min)\n"
                "2. Review the tree, then say **approve** to publish it as the shared default\n\n"
                "Once approved, I can scaffold any number of program folders instantly."
            )
        # Prerequisite 2: must know which program to scaffold
        if not decision.therapeutic_area or not decision.disease_type or not decision.drug_name:
            return _guide(
                "⚠️ **I need the program details to scaffold a folder.**\n\n"
                "Please specify the therapeutic area, disease, and drug:\n\n"
                "> *set up authoring for `<therapeutic_area> / <disease> / <drug>`*\n\n"
                "Example: *set up authoring for neurology / bells palsy / prednisolone*"
            )
        result = _ctd_post("/copy", {
            "session_id":       session_id,
            "bucket":           bkt,
            "therapeutic_area": decision.therapeutic_area,
            "disease_type":     decision.disease_type,
            "drug_name":        decision.drug_name,
            "state":            state,
        })

    elif intent == "write":
        _ta   = decision.therapeutic_area or _prog_state.get("therapeutic_area", "")
        _dis  = decision.disease_type     or _prog_state.get("disease_type", "")
        _drug = decision.drug_name        or _prog_state.get("drug_name", "")

        # Prerequisite 1: CTD structure must be approved / canonical exists
        if not _canonical_ready:
            return _guide(
                "⚠️ **No approved CTD structure found.**\n\n"
                "Content generation requires a published ICH CTD template. Here's the full workflow:\n\n"
                "1. Say **build the ICH CTD structure** — extracts the ICH M4 hierarchy (3–8 min)\n"
                "2. Say **approve** — publishes it as the shared default template\n"
                "3. Say **set up authoring for `<area> / <disease> / <drug>`** — scaffolds a program folder\n"
                "4. Upload the clinical CSV via the sidebar\n"
                "5. Say **generate content** — starts the AI content generation pipeline"
            )
        # Prerequisite 2: program folder must be scaffolded
        if not _scaffold_ready:
            return _guide(
                "⚠️ **No program folder scaffolded yet.**\n\n"
                "Before generating content, set up a program directory:\n\n"
                "→ Say **set up authoring for `<therapeutic_area> / <disease> / <drug>`**\n\n"
                "Example: *set up authoring for neurology / bells palsy / prednisolone*\n\n"
                "This copies the approved CTD template into a program-specific folder in GCS, "
                "and also maps any uploaded clinical CSV to that program."
            )
        # Prerequisite 3: must know which program to generate content for
        if not _ta or not _dis or not _drug:
            return _guide(
                "⚠️ **I couldn't identify the program** to generate content for.\n\n"
                "Please specify it explicitly:\n\n"
                "> *generate content for `<therapeutic_area> / <disease> / <drug>`*\n\n"
                "Example: *generate content for neurology / bells palsy / prednisolone*"
            )
        # Clinical CSV is optional — /trigger will ask for confirmation if missing
        result = _analyst_post("/trigger", {
            "session_id":       session_id,
            "bucket":           bkt,
            "therapeutic_area": _ta,
            "disease_type":     _dis,
            "drug_name":        _drug,
            "state":            state,
        })

    elif intent == "status":
        prog = state.get("content_program") or {}
        result = _ctd_get("/status_query", {
            "session_id":       session_id,
            "bucket":           bkt,
            "therapeutic_area": prog.get("therapeutic_area"),
            "disease_type":     prog.get("disease_type"),
            "drug_name":        prog.get("drug_name"),
            "msg_lower":        msg_lower,
        })

    elif intent == "rewrite_section":
        # Prerequisite: content generation must have run first (templates exist in GCS)
        if not _scaffold_ready:
            return _guide(
                "⚠️ **No program folder set up yet.**\n\n"
                "To rewrite a section the full pipeline must have run first:\n\n"
                "1. **Build + approve** the ICH CTD structure\n"
                "2. **Set up authoring** for the program\n"
                "3. **Generate content** — runs the template + writer pipeline\n"
                "4. Then say **rewrite section `<section_key>`**"
            )
        _r_ta   = decision.therapeutic_area or _prog_state.get("therapeutic_area", "")
        _r_dis  = decision.disease_type     or _prog_state.get("disease_type", "")
        _r_drug = decision.drug_name        or _prog_state.get("drug_name", "")
        if not _r_ta or not _r_dis or not _r_drug:
            return _guide(
                "⚠️ **I need the program details to rewrite a section.**\n\n"
                "> *rewrite section 2.5 for `<therapeutic_area> / <disease> / <drug>`*"
            )
        result = _ich4_post(
            _ICH4_WRITER_URL,
            "/write",
            {
                "program": {
                    "therapeutic_area": _r_ta,
                    "disease_type":     _r_dis,
                    "drug_name":        _r_drug,
                },
                "bucket_name": bkt,
                "sections":    decision.section_keys or [],
            },
            timeout=300,
        )
        if "reply" not in result:
            docs = result.get("documents") or []
            secs = ", ".join(d.get("section_key", "") for d in docs) if docs else "all sections"
            result = {
                "reply": (
                    f"✅ Rewrote **{len(docs)} section(s)** for `{_r_ta} / {_r_dis} / {_r_drug}`.\n\n"
                    f"Sections: `{secs}`"
                    if docs else
                    f"⚠️ No sections were written for `{_r_ta} / {_r_dis} / {_r_drug}`. "
                    "Check that templates exist in GCS (say **generate content** first)."
                ),
                "state_patch": {},
            }

    elif intent == "analyze_data":
        _a_ta   = decision.therapeutic_area or _prog_state.get("therapeutic_area", "")
        _a_dis  = decision.disease_type     or _prog_state.get("disease_type", "")
        _a_drug = decision.drug_name        or _prog_state.get("drug_name", "")
        if not _a_ta or not _a_dis or not _a_drug:
            return _guide(
                "⚠️ **I need the program details to analyse clinical data.**\n\n"
                "> *analyse clinical data for `<therapeutic_area> / <disease> / <drug>`*\n\n"
                "Note: you must also upload the clinical CSV via the sidebar first."
            )
        result = _analyst_post(
            "/analyze",
            {
                "therapeutic_area": _a_ta,
                "disease_type":     _a_dis,
                "drug_name":        _a_drug,
                "bucket":           bkt,
                "question":         message,
            },
        )

    else:  # help / unknown — handled locally, no API call
        # Show current workflow status + guidance based on where the user is
        _step = "unknown"
        if not _canonical_ready:
            _step = "step1"
        elif not _scaffold_ready:
            _step = "step2"
        else:
            _step = "step3"

        _status_hint = {
            "step1": (
                "📍 **Current status:** No approved CTD template yet.\n"
                "→ Say **build the ICH CTD structure** to start."
            ),
            "step2": (
                "📍 **Current status:** CTD template approved. No program scaffolded yet.\n"
                "→ Say **set up authoring for `<area> / <disease> / <drug>`** to create a program folder."
            ),
            "step3": (
                "📍 **Current status:** Program folder ready.\n"
                "→ Upload clinical CSV via sidebar, then say **generate content**."
            ),
        }.get(_step, "")

        result = {
            "reply": (
                "I'm your **Regulatory Authoring Assistant**. Here's the full workflow:\n\n"
                "**Step 1 — Build the ICH CTD structure**\n"
                "- *build the ICH CTD structure* — queries the ICH M4 guidelines index (3–8 min)\n"
                "- *approve* — publishes it as the shared default template\n\n"
                "**Step 2 — Set up a program folder**\n"
                "- *set up authoring for neurology / bells palsy / prednisolone*\n"
                "  → Copies the approved template to a program-specific GCS folder\n\n"
                "**Step 3 — Upload clinical data** *(optional but recommended)*\n"
                "- Upload the trial CSV via the 📎 sidebar — it maps to the program automatically\n\n"
                "**Step 4 — Generate content**\n"
                "- *generate content* — runs the template engine + AI writer pipeline\n"
                "  → Resolves `{{placeholders}}` from clinical data, generates all CTD sections\n\n"
                "**Step 5 — Review & refine**\n"
                "- *content status* — check generation progress\n"
                "- *rewrite section 2.5* — regenerate a specific section\n"
                "- *what does the trial data show?* — query clinical data\n\n"
                f"---\n{_status_hint}"
            ),
            "state_patch": {},
        }

    state = {**state, **result.get("state_patch", {})}
    history.append({"role": "assistant", "content": result.get("reply", "_(no reply)_")})
    return history, state, gr.update(value="")


# ── Auto-poll timer ───────────────────────────────────────────────────────────

def _auto_poll(history: list, state: dict, bucket: str):
    """Called every 20 s by gr.Timer — surfaces job completions automatically."""
    st  = state or {}
    bkt = (bucket or "").strip() or _DEFAULT_BUCKET
    sid = st.get("session_id")
    if not sid:
        return history, state

    prog = st.get("content_program") or {}
    data = _ctd_get("/status", {
        "session_id":       sid,
        "bucket":           bkt,
        "therapeutic_area": prog.get("therapeutic_area", "") or None,
        "disease_type":     prog.get("disease_type", "") or None,
        "drug_name":        prog.get("drug_name", "") or None,
    })
    ext  = data.get("extraction") or {}
    cont = data.get("content") or {}

    new_history = list(history or [])
    new_state   = dict(st)

    # ── Extraction complete ───────────────────────────────────────────────────
    if not st.get("folder_paths") and ext.get("status") == "done":
        sess  = _ctd_get("/session", {"session_id": sid, "bucket": bkt})
        paths = sess.get("folder_paths", [])
        if paths:
            tree = _fmt_tree_paths(paths)
            new_state = {**new_state, "folder_paths": paths}
            if sess.get("ctd_output"):
                new_state["ctd_output"] = sess["ctd_output"]
            new_history.append({"role": "assistant", "content": (
                "✅ **Extraction complete!** Here's the ICH M4(R4) CTD folder structure:\n\n"
                f"```\n{tree}\n```\n\n"
                "You can copy it to a program now — tell me the therapeutic area, disease, and drug.\n"
                "Or say **approve** to commit it as the shared default canonical template."
            )})
            return new_history, new_state

    # ── Content generation complete / failed / timed_out ─────────────────────
    if prog and st.get("content_run_id") and cont:
        cstatus = cont.get("status")
        ta, dis, drg = prog.get("therapeutic_area", ""), prog.get("disease_type", ""), prog.get("drug_name", "")

        if cstatus == "done":
            sections = cont.get("sections_written", "?")
            val_pass = cont.get("validation_passed", False)
            val_sum  = cont.get("validation_summary", "")
            new_state = {**new_state, "content_run_id": None}
            new_history.append({"role": "assistant", "content": (
                f"✅ **Content generation complete!**\n\n"
                f"- **{sections} sections** written to GCS\n"
                f"- {'✅' if val_pass else '⚠️'} Validation: {val_sum or ('passed' if val_pass else 'issues found')}\n\n"
                f"Documents saved to `gs://{bkt}/therapeutic-area/"
                f"{ta.replace(' ','_').lower()}/{dis.replace(' ','_').lower()}/{drg.replace(' ','_').lower()}/ctd/`\n\n"
                "Say **generate content** to regenerate with updated clinical data."
            )})
            return new_history, new_state

        elif cstatus == "failed":
            new_state = {**new_state, "content_run_id": None}
            new_history.append({"role": "assistant", "content": (
                f"❌ **Content generation failed** — {cont.get('error', 'unknown error')}\n\n"
                "Say **generate content** to try again."
            )})
            return new_history, new_state

        elif cstatus == "timed_out":
            age = cont.get("age_minutes", "?")
            new_state = {**new_state, "content_run_id": None}
            new_history.append({"role": "assistant", "content": (
                f"⚠️ **Content generation timed out** — started {age} minutes ago.\n\n"
                "Say **generate content** to start a fresh run."
            )})
            return new_history, new_state

    return history, state


# ── Clinical CSV upload ───────────────────────────────────────────────────────

def _upload_clinical_csv(csv_file, ta: str, dis: str, drug: str, bucket: str) -> str:
    ta   = (ta   or "").strip()
    dis  = (dis  or "").strip()
    drug = (drug or "").strip()
    if not ta or not dis or not drug:
        return "⚠️ Please fill in Therapeutic Area, Disease, and Drug Name before uploading."
    if csv_file is None:
        return "⚠️ No file selected."
    local_path = csv_file if isinstance(csv_file, str) else csv_file.get("path", "")
    if not local_path:
        return "⚠️ Could not read uploaded file path."
    bkt = (bucket or "").strip() or _DEFAULT_BUCKET
    try:
        with open(local_path, "rb") as f:
            resp = requests.post(
                f"{_ICH4_WRITER_URL.rstrip('/')}/clinical-data/upload",
                data={"therapeutic_area": ta, "disease_type": dis, "drug_name": drug, "bucket_name": bkt},
                files={"file": (os.path.basename(local_path), f, "text/csv")},
                headers=_oidc_headers(_ICH4_WRITER_URL),
                timeout=120,
            )
        resp.raise_for_status()
        data = resp.json()
        secs = data.get("sections", [])
        return (
            f"✅ **{data.get('filename', local_path)}** registered successfully!\n\n"
            f"- Mapped columns: **{data.get('columns', 0)}**\n"
            f"- CTD sections: `{'`, `'.join(secs)}`\n"
            f"- Total datasets for **{drug}** / **{dis}** / **{ta}**: **{data.get('total_sources', 1)}**\n\n"
            "You can now say **generate content** in the chat."
        )
    except Exception as exc:
        return f"❌ Upload failed: {exc}"


def _update_upload_panel(state: dict):
    prog = (state or {}).get("content_program")
    if prog:
        return (
            gr.update(visible=True),
            gr.update(value=prog.get("therapeutic_area", "")),
            gr.update(value=prog.get("disease_type", "")),
            gr.update(value=prog.get("drug_name", "")),
        )
    return gr.update(visible=True), gr.update(value=""), gr.update(value=""), gr.update(value="")


def _list_documents(ta: str, dis: str, drug: str, bucket: str) -> tuple:
    """Fetch document list from ich4-writer and return dropdown choices + status."""
    if not ta or not dis or not drug:
        return gr.update(choices=[], value=None), "⚠️ Fill in Therapeutic Area, Disease, and Drug Name first."
    data = _writer_get("/documents", {
        "therapeutic_area": ta,
        "disease_type": dis,
        "drug_name": drug,
        "bucket_name": bucket or _DEFAULT_BUCKET,
    })
    docs = data.get("documents", [])
    if not docs:
        return gr.update(choices=[], value=None), "No generated documents found for this program yet."
    choices = [
        (f"{d['module']} › {d['section_label']}", f"{d['module']}|{d['section_key']}")
        for d in docs
    ]
    return gr.update(choices=choices, value=None), f"✅ Found **{len(docs)}** sections."


def _read_document(selection: str, ta: str, dis: str, drug: str, bucket: str) -> str:
    """Fetch and return the markdown content of the selected section."""
    if not selection:
        return ""
    parts = selection.split("|", 1)
    if len(parts) != 2:
        return "⚠️ Invalid selection."
    module, section_key = parts
    data = _writer_get("/documents/read", {
        "therapeutic_area": ta,
        "disease_type": dis,
        "drug_name": drug,
        "module": module,
        "section_key": section_key,
        "bucket_name": bucket or _DEFAULT_BUCKET,
    })
    content = data.get("content", "")
    if not content:
        return "⚠️ Could not load section content."
    return content


# ── Gradio UI ─────────────────────────────────────────────────────────────────

_CSS = """
footer { display: none !important; }
:root, html.dark {
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
body, .gradio-container, html.dark .gradio-container, html.dark body {
    background-color: #ffffff !important; background: #ffffff !important; color: #1a1a2e !important;
}
html.dark .gradio-container, html.dark .gradio-container p, html.dark .gradio-container span,
html.dark .gradio-container div, html.dark .gradio-container label, html.dark .gradio-container li,
html.dark .gradio-container td, html.dark .gradio-container th, html.dark .gradio-container h1,
html.dark .gradio-container h2, html.dark .gradio-container h3, html.dark .gradio-container h4 {
    color: #1a1a2e !important; background-color: transparent;
}
html.dark .block, html.dark .panel, html.dark .form, html.dark fieldset { background-color: #ffffff !important; }
html.dark input, html.dark textarea, html.dark select { background-color: #ffffff !important; color: #1a1a2e !important; }
html.dark .bubble-wrap { background-color: #f5f8fd !important; }
html.dark .prose, html.dark .prose * { color: #1a1a2e !important; }
html.dark code, html.dark pre { background-color: #eef3fb !important; color: #1a3a6a !important; }
.rp-header { padding: 16px 0 8px 0; border-bottom: 2px solid #1a4f8a; margin-bottom: 16px; }
.rp-header h1 { font-size: 1.55em; font-weight: 700; color: #1a4f8a !important; margin: 0 0 2px 0; }
.rp-header p  { font-size: 0.88em; color: #444 !important; margin: 0; }
.rp-badges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.rp-badge { font-size: 0.78em; font-weight: 600; padding: 3px 10px; border-radius: 12px; background: #eef3fb !important; color: #1a4f8a !important; border: 1px solid #b3c9e8; }
.chatbot-wrap { border-radius: 8px; border: 1px solid #d1dce8 !important; background: #ffffff !important; }
.qa-row { margin-top: 6px; margin-bottom: 2px; }
.qa-row button { font-size: 0.80em !important; padding: 4px 12px !important; border-radius: 20px !important; font-weight: 500 !important; background: #eef3fb !important; color: #1a4f8a !important; border: 1px solid #b3c9e8 !important; cursor: pointer; }
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

_HEAD = """
<script>
(function() {
    var html = document.documentElement;
    html.classList.remove('dark');
    html.style.colorScheme = 'light';
    new MutationObserver(function() {
        if (html.classList.contains('dark')) { html.classList.remove('dark'); html.style.colorScheme = 'light'; }
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

with gr.Blocks(title="Regulatory Authoring Platform", css=_CSS, theme=_theme, head=_HEAD) as demo:

    with gr.Column(elem_classes=["rp-header"]):
        gr.HTML(
            "<h1>&#9878; Regulatory Authoring Platform</h1>"
            "<p>AI-assisted authoring for ICH M4(R4) Common Technical Documents &mdash; "
            "from structure setup to section-level content generation.</p>"
        )

    with gr.Row(elem_classes=["rp-badges"]):
        for _badge in ["📋 ICH M4 CTD Structure", "📝 Section Content Generation",
                        "🔬 Clinical Data Integration", "✅ Regulatory Validation", "☁️ GCS Document Store"]:
            gr.HTML(f'<span class="rp-badge">{_badge}</span>')

    _state_init      = gr.State(value={})
    _browser_session = gr.BrowserState("")

    chatbot = gr.Chatbot(
        value=[], label="", elem_classes=["chatbot-wrap"], height=500,
        show_label=False, avatar_images=(None, None), render_markdown=True,
    )

    with gr.Row():
        msg_input = gr.Textbox(
            placeholder="Describe what you need — e.g. set up authoring for oncology / lung cancer / carboplatin",
            label="", lines=1, max_lines=4, autofocus=True, show_label=False,
        )
        send_btn = gr.Button("➤", variant="primary", scale=0, min_width=60)

    _example_btns: list = []
    with gr.Row(elem_classes=["qa-row"]):
        for ex in _EXAMPLES:
            _example_btns.append((gr.Button(ex, size="sm"), ex))

    with gr.Accordion("⚙  Configuration", open=False):
        with gr.Row():
            email_input   = gr.Textbox(label="Compliance officer email", placeholder="officer@example.com", scale=2)
            bucket_input  = gr.Textbox(label="GCS document bucket", value=_DEFAULT_BUCKET, scale=2)

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
            upload_file = gr.File(label="Clinical CSV file", file_types=[".csv"], scale=3)
            upload_btn  = gr.Button("Upload & Register", variant="primary", scale=1)
        upload_status = gr.Markdown(value="", label="")

        upload_btn.click(
            fn=_upload_clinical_csv,
            inputs=[upload_file, upload_ta, upload_dis, upload_drug, bucket_input],
            outputs=upload_status,
        )

    with gr.Accordion("📄  Generated Documents", open=False) as _docs_accordion:
        gr.Markdown(
            "Browse and read generated CTD section content. "
            "Enter the program details and click **Load Sections** to list available documents."
        )
        with gr.Row():
            docs_ta   = gr.Textbox(label="Therapeutic Area",  placeholder="e.g. neurology",    scale=1)
            docs_dis  = gr.Textbox(label="Disease",           placeholder="e.g. bells palsy",  scale=1)
            docs_drug = gr.Textbox(label="Drug Name",         placeholder="e.g. prednisolone", scale=1)
        with gr.Row():
            docs_load_btn = gr.Button("Load Sections", variant="primary", scale=1)
        docs_status = gr.Markdown(value="")
        docs_dropdown = gr.Dropdown(
            choices=[], label="Select a section to read", interactive=True
        )
        docs_content = gr.Markdown(value="", label="", elem_classes=["chatbot-wrap"])

        docs_load_btn.click(
            fn=_list_documents,
            inputs=[docs_ta, docs_dis, docs_drug, bucket_input],
            outputs=[docs_dropdown, docs_status],
        )
        docs_dropdown.change(
            fn=_read_document,
            inputs=[docs_dropdown, docs_ta, docs_dis, docs_drug, bucket_input],
            outputs=docs_content,
        )

    def _on_load(bucket: str, browser_session: str):
        return _initial_state(bucket or _DEFAULT_BUCKET, browser_session)

    demo.load(fn=_on_load, inputs=[bucket_input, _browser_session],
              outputs=[chatbot, _state_init, _browser_session])

    _state_init.change(
        fn=_update_upload_panel,
        inputs=[_state_init],
        outputs=[_upload_accordion, upload_ta, upload_dis, upload_drug],
    )
    _state_init.change(
        fn=lambda s: (
            gr.update(value=(s or {}).get("content_program", {}).get("therapeutic_area", "")),
            gr.update(value=(s or {}).get("content_program", {}).get("disease_type", "")),
            gr.update(value=(s or {}).get("content_program", {}).get("drug_name", "")),
        ),
        inputs=[_state_init],
        outputs=[docs_ta, docs_dis, docs_drug],
    )

    msg_input.submit(
        chat,
        inputs=[msg_input, chatbot, _state_init, bucket_input, email_input],
        outputs=[chatbot, _state_init, msg_input],
    )
    send_btn.click(
        chat,
        inputs=[msg_input, chatbot, _state_init, bucket_input, email_input],
        outputs=[chatbot, _state_init, msg_input],
    )
    for _btn, _ex in _example_btns:
        (_btn
            .click(fn=lambda m=_ex: m, outputs=msg_input)
            .then(fn=chat,
                  inputs=[msg_input, chatbot, _state_init, bucket_input, email_input],
                  outputs=[chatbot, _state_init, msg_input]))

    poll_timer = gr.Timer(value=20, active=True)
    poll_timer.tick(
        fn=_auto_poll,
        inputs=[chatbot, _state_init, bucket_input],
        outputs=[chatbot, _state_init],
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    demo.launch(server_name="0.0.0.0", server_port=port)
