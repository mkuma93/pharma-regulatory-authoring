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

from main import CoordinatorDecision, run_coordinator  # LangGraph coordinator

# ── Config ────────────────────────────────────────────────────────────────────

_CTD_API_URL             = os.environ.get("CTD_API_URL", "http://localhost:8081")
_ICH4_ORCHESTRATOR_URL   = os.environ.get("ICH4_ORCHESTRATOR_URL", "http://localhost:8082")
_ICH4_WRITER_URL         = os.environ.get("ICH4_WRITER_URL", "http://localhost:8083")
_CLINICAL_ANALYST_URL    = os.environ.get("CLINICAL_ANALYST_URL", "http://localhost:8084")
_DEFAULT_BUCKET          = os.environ.get("GCS_BUCKET", "pharma-reguatory-author-life-science")

# ── API client helpers ────────────────────────────────────────────────────────

def _ctd_post(path: str, payload: dict, timeout: int = 60) -> dict:
    """POST to ctd-api and return parsed JSON. Returns error reply on failure."""
    url = f"{_CTD_API_URL.rstrip('/')}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"reply": f"❌ CTD API error (`{path}`): {exc}", "state_patch": {}}


def _ctd_get(path: str, params: dict, timeout: int = 30) -> dict:
    """GET from ctd-api and return parsed JSON. Returns {} on failure."""
    url = f"{_CTD_API_URL.rstrip('/')}{path}"
    try:
        resp = requests.get(url, params={k: v for k, v in params.items() if v is not None},
                            timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}


def _ich4_post(base_url: str, path: str, payload: dict, timeout: int = 300) -> dict:
    """POST to an ICH4 service (orchestrator or writer). Returns error reply on failure."""
    url = f"{base_url.rstrip('/')}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"reply": f"❌ ICH4 service error (`{path}`): {exc}", "state_patch": {}}


def _analyst_post(path: str, payload: dict, timeout: int = 120) -> dict:
    """POST to the clinical-analyst service. Returns error reply on failure."""
    url = f"{_CLINICAL_ANALYST_URL.rstrip('/')}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
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
        "session_id":       sid,
        "bucket":           bkt,
        "folder_paths":     paths,
        "approved":         approved,
        "canonical_exists": data.get("canonical_exists", False),
        "ctd_output":       data.get("ctd_output"),
        "content_program":  data.get("content_program"),
        "content_run_id":   data.get("content_run_id"),
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
    """
    history    = list(history or [])
    state      = state or {}
    bkt        = (bucket or "").strip() or _DEFAULT_BUCKET
    msg_lower  = message.strip().lower()
    session_id = state.get("session_id", "default")

    history.append({"role": "user", "content": message})

    # ── Short-circuit: disapproval feedback loop ──────────────────────────────#
    _EXTRACT_KW = ("re-extract", "reextract", "re extract", "extract", "rebuild", "refresh")
    _ESCAPE_KW  = ("generate", "content", "status", "copy", "write", "approve",
                   "scaffold", "set up", "setup", "template", "analyse", "analyze",
                   "rewrite", "section", "data")
    is_extract = any(msg_lower.startswith(kw) or msg_lower == kw for kw in _EXTRACT_KW)
    is_escape  = any(kw in msg_lower for kw in _ESCAPE_KW)

    if state.get("awaiting_feedback") and not is_extract and not is_escape:
        history.append({"role": "assistant", "content": "_Thinking…_"})
        yield history, state, gr.update(value="")
        result = _ctd_post("/refine", {
            "session_id":     session_id,
            "bucket":         bkt,
            "feedback":       message,
            "prior_feedback": state.get("disapproval_feedback", "") or "",
            "ctd_output":     state.get("ctd_output"),
        })
        history[-1] = {"role": "assistant", "content": result.get("reply", "_(no reply)_")}
        state = {**state, **result.get("state_patch", {})}
        yield history, state, gr.update(value="")
        return

    if state.get("awaiting_feedback") and (is_extract or is_escape):
        state = {**state, "awaiting_feedback": False}

    # ── Short-circuit: awaiting write confirmation (no clinical data) ─────────
    if state.get("awaiting_write_confirm"):
        pending = state["awaiting_write_confirm"]
        ta_p, dis_p, drug_p = pending.get("ta", ""), pending.get("dis", ""), pending.get("drug", "")
        _YES = ("yes", "proceed", "go ahead", "ok", "sure", "continue", "confirm")
        _NO  = ("no", "cancel", "stop", "skip")

        if any(msg_lower.startswith(w) for w in _YES):
            history.append({"role": "assistant", "content": "_Queuing…_"})
            yield history, state, gr.update(value="")
            result = _analyst_post("/trigger", {
                "session_id":        session_id,
                "bucket":            bkt,
                "therapeutic_area":  ta_p,
                "disease_type":      dis_p,
                "drug_name":         drug_p,
                "force_no_clinical": True,
                "state":             state,
            })
            history[-1] = {"role": "assistant", "content": result.get("reply", "_(no reply)_")}
            state = {**state, **result.get("state_patch", {})}
            yield history, state, gr.update(value="")
            return

        elif any(msg_lower.startswith(w) for w in _NO):
            history.append({"role": "assistant", "content": (
                "OK, content generation cancelled. "
                "Register a clinical CSV for this program first, then say **generate content** again."
            )})
            state = {**state, "awaiting_write_confirm": None}
            yield history, state, gr.update(value="")
            return
        else:
            state = {**state, "awaiting_write_confirm": None}

    # ── Show thinking indicator while coordinator runs ────────────────────────
    history.append({"role": "assistant", "content": "_Thinking…_"})
    yield history, state, gr.update(value="")

    # ── Coordinator: understand intent + check prerequisites ──────────────────
    decision: CoordinatorDecision = run_coordinator(message, state)
    history.pop()  # remove placeholder

    # ── Clarify: coordinator composes the reply — no downstream API call ──────
    if decision.outcome == "clarify":
        history.append({"role": "assistant", "content": decision.reply or "Could you clarify what you need?"})
        yield history, state, gr.update(value="")
        return

    # ── Proceed: route to the correct API ────────────────────────────────────
    intent = decision.intent
    result: dict = {}

    if intent == "extract":
        result = _ctd_post("/extract", {
            "session_id":     session_id,
            "bucket":         bkt,
            "reviewer_email": reviewer_email or None,
            "force":          decision.force_reextract,
        })

    elif intent == "approve":
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
        result = _ctd_post("/disapprove", {
            "session_id": session_id,
            "feedback":   decision.feedback,
        })

    elif intent == "copy":
        result = _ctd_post("/copy", {
            "session_id":       session_id,
            "bucket":           bkt,
            "therapeutic_area": decision.therapeutic_area,
            "disease_type":     decision.disease_type,
            "drug_name":        decision.drug_name,
            "state":            state,
        })

    elif intent == "write":
        result = _analyst_post("/trigger", {
            "session_id":              session_id,
            "bucket":                  bkt,
            "therapeutic_area":        decision.therapeutic_area,
            "disease_type":            decision.disease_type,
            "drug_name":               decision.drug_name,
            "program_scaffold_exists": state.get("program_scaffold_exists", False),
            "state":                   state,
        })

    elif intent == "status":
        prog = state.get("content_program") or {}
        result = _ctd_get("/status_query", {
            "session_id": session_id,
            "bucket":     bkt,
            "ta":         prog.get("ta"),
            "dis":        prog.get("dis"),
            "drug":       prog.get("drug"),
            "msg_lower":  msg_lower,
        })

    elif intent == "generate_template":
        # Generate ICH-grounded section templates for the program (no write)
        result = _ich4_post(
            _ICH4_ORCHESTRATOR_URL,
            "/generate",
            {
                "program": {
                    "therapeutic_area": decision.therapeutic_area,
                    "disease_type":     decision.disease_type,
                    "drug_name":        decision.drug_name,
                },
                "include_clinical_data": True,
                "include_ich_context":   True,
                "module_filter":         decision.module_filter or [],
            },
            timeout=300,
        )
        # Wrap the orchestrator response into the standard reply/state_patch shape
        if "reply" not in result:
            templates = result.get("templates") or []
            ta   = decision.therapeutic_area
            dis  = decision.disease_type
            drug = decision.drug_name
            n    = len(templates)
            result = {
                "reply": (
                    f"✅ Generated **{n} templates** for `{ta} / {dis} / {drug}`.\n\n"
                    "Templates are saved in GCS. Say **generate content** to run the full writing pipeline."
                    if n else
                    f"⚠️ No templates were returned for `{ta} / {dis} / {drug}`. "
                    "Check that the program or clinical data exists in GCS."
                ),
                "state_patch": {"content_program": {"ta": ta, "dis": dis, "drug": drug}},
            }

    elif intent == "rewrite_section":
        # Regenerate specific CTD section documents via the writer service
        result = _ich4_post(
            _ICH4_WRITER_URL,
            "/write",
            {
                "program": {
                    "therapeutic_area": decision.therapeutic_area,
                    "disease_type":     decision.disease_type,
                    "drug_name":        decision.drug_name,
                },
                "bucket_name": bkt,
                "sections":    decision.section_keys or [],
            },
            timeout=300,
        )
        if "reply" not in result:
            ta   = decision.therapeutic_area
            dis  = decision.disease_type
            drug = decision.drug_name
            docs = result.get("documents") or []
            secs = ", ".join(d.get("section_key", "") for d in docs) if docs else "all sections"
            result = {
                "reply": (
                    f"✅ Rewrote **{len(docs)} section(s)** for `{ta} / {dis} / {drug}`.\n\n"
                    f"Sections: `{secs}`"
                    if docs else
                    f"⚠️ No sections were written for `{ta} / {dis} / {drug}`. "
                    "Check that templates exist in GCS (say **generate templates** first)."
                ),
                "state_patch": {},
            }

    elif intent == "analyze_data":
        # Run clinical data analysis for the program
        result = _analyst_post(
            "/analyze",
            {
                "therapeutic_area": decision.therapeutic_area,
                "disease_type":     decision.disease_type,
                "drug_name":        decision.drug_name,
                "bucket":           bkt,
                "question":         message,
            },
        )

    else:  # help / unknown — handled locally, no API call
        result = {
            "reply": (
                "I can help you with the **Regulatory Authoring Platform**. Here are examples:\n\n"
                "**CTD structure**\n"
                "- *Generate the ICH CTD structure from the guidelines*\n"
                "- *Approve this structure* • *Re-extract with updates*\n"
                "- *Set up neurology / bells palsy / prednisolone*\n\n"
                "**Content generation**\n"
                "- *Generate content for neurology / bells palsy / prednisolone*\n"
                "- *Preview templates for module 5* • *Rewrite section 2.5*\n\n"
                "**Clinical data**\n"
                "- *Analyse the clinical data for neurology / bells palsy / prednisolone*\n"
                "- *What does the trial data show for prednisolone?*\n\n"
                "**Status**\n"
                "- *What is currently running?* • *What have you loaded so far?*"
            ),
            "state_patch": {},
        }

    state = {**state, **result.get("state_patch", {})}
    history.append({"role": "assistant", "content": result.get("reply", "_(no reply)_")})
    yield history, state, gr.update(value="")


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
        "session_id": sid,
        "bucket":     bkt,
        "ta":         prog.get("ta", "") or None,
        "dis":        prog.get("dis", "") or None,
        "drug":       prog.get("drug", "") or None,
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
        ta, dis, drg = prog.get("ta", ""), prog.get("dis", ""), prog.get("drug", "")

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
            gr.update(value=prog.get("ta", "")),
            gr.update(value=prog.get("dis", "")),
            gr.update(value=prog.get("drug", "")),
        )
    return gr.update(visible=True), gr.update(value=""), gr.update(value=""), gr.update(value="")


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

    msg_input = gr.Textbox(
        placeholder="Describe what you need — e.g. set up authoring for oncology / lung cancer / carboplatin",
        label="", lines=1, max_lines=4, autofocus=True, show_label=False,
    )

    with gr.Row(elem_classes=["qa-row"]):
        for ex in _EXAMPLES:
            gr.Button(ex, size="sm").click(fn=lambda m=ex: m, outputs=msg_input)

    with gr.Accordion("⚙  Configuration", open=False):
        with gr.Row():
            email_input   = gr.Textbox(label="Compliance officer email", placeholder="officer@example.com", scale=2)
            bucket_input  = gr.Textbox(label="GCS document bucket", value=_DEFAULT_BUCKET, scale=2)
            api_url_input = gr.Textbox(label="CTD API URL", value=_CTD_API_URL, scale=3)

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

    def _on_load(bucket: str, browser_session: str):
        return _initial_state(bucket or _DEFAULT_BUCKET, browser_session)

    demo.load(fn=_on_load, inputs=[bucket_input, _browser_session],
              outputs=[chatbot, _state_init, _browser_session])

    _state_init.change(
        fn=_update_upload_panel,
        inputs=[_state_init],
        outputs=[_upload_accordion, upload_ta, upload_dis, upload_drug],
    )

    msg_input.submit(
        chat,
        inputs=[msg_input, chatbot, _state_init, bucket_input, email_input],
        outputs=[chatbot, _state_init, msg_input],
    )

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
