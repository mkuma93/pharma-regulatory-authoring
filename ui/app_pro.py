"""
ui/app_pro.py

Professional, step-by-step guided UI for the Regulatory Authoring Platform.

Designed for regulatory authors who are not technical. Each step clearly explains
what to do, why it matters, and what will happen — no jargon, no lost feeling.

Workflow
────────
  Step 1 → Build Document Framework   (ICH M4 structure extraction + approval)
  Step 2 → Set Up Drug Program         (scaffold program folder in GCS)
  Step 3 → Register Trial Data         (upload clinical CSV)
  Step 4 → Generate Documents          (AI-powered CTD section writing)
  Step 5 → Review & Export             (read and browse generated sections)

Environment variables
─────────────────────
  CTD_API_URL           — ctd-api Cloud Run URL
  ICH4_WRITER_URL       — ich4-writer Cloud Run URL
  CLINICAL_ANALYST_URL  — clinical-analyst Cloud Run URL
  GCS_BUCKET            — default GCS bucket
  PORT                  — injected by Cloud Run
  OPENAI_API_KEY        — coordinator LLM
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime

import gradio as gr
import requests

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

# ── Config ─────────────────────────────────────────────────────────────────────
_CTD_API_URL          = os.environ.get("CTD_API_URL",          "http://localhost:8081")
_ICH4_WRITER_URL      = os.environ.get("ICH4_WRITER_URL",      "http://localhost:8083")
_CLINICAL_ANALYST_URL = os.environ.get("CLINICAL_ANALYST_URL", "http://localhost:8084")
_DEFAULT_BUCKET       = os.environ.get("GCS_BUCKET",           "pharma-reguatory-author-life-science")


# ── OIDC ───────────────────────────────────────────────────────────────────────
def _oidc_headers(base_url: str) -> dict[str, str]:
    try:
        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2.id_token import fetch_id_token
        from urllib.parse import urlparse
        p = urlparse(base_url)
        audience = f"{p.scheme}://{p.netloc}"
        token = fetch_id_token(AuthRequest(), audience)
        return {"Authorization": f"Bearer {token}"}
    except Exception:
        return {}


# ── API helpers ────────────────────────────────────────────────────────────────
def _ctd_post(path: str, payload: dict, timeout: int = 60) -> dict:
    url = f"{_CTD_API_URL.rstrip('/')}{path}"
    try:
        r = requests.post(url, json=payload, headers=_oidc_headers(_CTD_API_URL), timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"reply": f"Service error: {exc}", "state_patch": {}}


def _ctd_get(path: str, params: dict, timeout: int = 30) -> dict:
    url = f"{_CTD_API_URL.rstrip('/')}{path}"
    try:
        r = requests.get(url, params={k: v for k, v in params.items() if v is not None},
                         headers=_oidc_headers(_CTD_API_URL), timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _writer_get(path: str, params: dict, timeout: int = 60) -> dict:
    url = f"{_ICH4_WRITER_URL.rstrip('/')}{path}"
    try:
        r = requests.get(url, params={k: v for k, v in params.items() if v is not None},
                         headers=_oidc_headers(_ICH4_WRITER_URL), timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _analyst_post(path: str, payload: dict, timeout: int = 120) -> dict:
    url = f"{_CLINICAL_ANALYST_URL.rstrip('/')}{path}"
    try:
        r = requests.post(url, json=payload, headers=_oidc_headers(_CLINICAL_ANALYST_URL), timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"reply": f"Service error: {exc}", "state_patch": {}}


# ── Timestamp helper ───────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(existing: str, message: str) -> str:
    """Append a timestamped line to the activity log."""
    line = f"[{_ts()}]  {message}"
    return (existing.strip() + "\n" + line).strip() if existing.strip() else line


# ── Session state init ─────────────────────────────────────────────────────────
def _init_session(bucket: str, browser_session: str):
    sid  = browser_session or str(uuid.uuid4())
    bkt  = (bucket or "").strip() or _DEFAULT_BUCKET
    data = _ctd_get("/session", {"session_id": sid, "bucket": bkt})

    state = {
        "session_id":              sid,
        "bucket":                  bkt,
        "folder_paths":            data.get("folder_paths", []),
        "approved":                data.get("approved", False),
        "canonical_exists":        data.get("canonical_exists", False),
        "program_scaffold_exists": data.get("program_scaffold_exists", False),
        "content_program":         data.get("content_program"),
        "content_run_id":          data.get("content_run_id"),
        "ctd_output":              data.get("ctd_output"),
        "extraction_in_progress":  False,
        "generation_in_progress":  False,
    }
    return state, sid


# ── Status badge HTML helpers ──────────────────────────────────────────────────
def _badge(text: str, color: str) -> str:
    colors = {
        "green":  "#d1fae5:#065f46",
        "yellow": "#fef9c3:#854d0e",
        "red":    "#fee2e2:#991b1b",
        "blue":   "#dbeafe:#1e40af",
        "grey":   "#f3f4f6:#374151",
    }
    bg, fg = colors.get(color, colors["grey"]).split(":")
    return (
        f'<span style="background:{bg};color:{fg};padding:4px 12px;border-radius:20px;'
        f'font-size:0.82em;font-weight:600;">{text}</span>'
    )


def _step_banner(title: str, subtitle: str, status_html: str) -> str:
    return f"""
<div style="background:#f8fafc;border-left:4px solid #1a4f8a;border-radius:6px;
            padding:16px 20px;margin-bottom:20px;">
  <div style="font-size:1.15em;font-weight:700;color:#1a1a2e;margin-bottom:4px;">{title}</div>
  <div style="font-size:0.88em;color:#555;margin-bottom:10px;">{subtitle}</div>
  <div>{status_html}</div>
</div>"""


def _info_box(text: str) -> str:
    return (
        f'<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;'
        f'padding:12px 16px;font-size:0.86em;color:#1e40af;margin-bottom:12px;">'
        f'ℹ️ &nbsp;{text}</div>'
    )


# ── Step 1: Framework status HTML ──────────────────────────────────────────────
def _framework_status_html(state: dict) -> str:
    approved  = state.get("approved") or state.get("canonical_exists")
    has_paths = bool(state.get("folder_paths"))
    in_prog   = state.get("extraction_in_progress", False)

    if approved:
        status = _badge("✅  Document framework published", "green")
        note   = "The ICH M4(R4) folder hierarchy is live and shared across all drug programs."
    elif in_prog:
        status = _badge("⏳  Building framework — please wait (3–8 min)", "yellow")
        note   = "The system is querying ICH M4 guidelines. This page will update automatically."
    elif has_paths:
        status = _badge("⚠️  Framework built but not yet published", "yellow")
        note   = "Review the structure below, then click Publish Framework."
    else:
        status = _badge("○  Not started", "grey")
        note   = "Click Build Document Framework to begin. This is a one-time setup."

    return _step_banner(
        "Step 1 — Document Framework",
        "Build and publish the ICH M4(R4) folder hierarchy. Done once; shared across all programs.",
        f"{status}<div style='font-size:0.84em;color:#555;margin-top:8px;'>{note}</div>",
    )


# ── Step 2: Program status HTML ────────────────────────────────────────────────
def _program_status_html(state: dict) -> str:
    prog    = state.get("content_program") or {}
    ready   = state.get("program_scaffold_exists") or bool(prog)
    approved = state.get("approved") or state.get("canonical_exists")

    if not approved:
        status = _badge("🔒  Complete Step 1 first", "grey")
        note   = "The document framework must be published before creating a program folder."
    elif ready:
        ta, dis, drug = prog.get("therapeutic_area","?"), prog.get("disease_type","?"), prog.get("drug_name","?")
        status = _badge(f"✅  Active: {ta.title()} / {dis.title()} / {drug.title()}", "green")
        note   = "Program folder is set up in the document store."
    else:
        status = _badge("○  Not set up yet", "grey")
        note   = "Enter the drug program details below and click Set Up Program."

    return _step_banner(
        "Step 2 — Drug Program",
        "Create a dedicated folder for all regulatory documents for this drug and indication.",
        f"{status}<div style='font-size:0.84em;color:#555;margin-top:8px;'>{note}</div>",
    )


# ── Step 3: Clinical data status HTML ─────────────────────────────────────────
def _clinical_status_html(upload_msg: str, state: dict) -> str:
    prog  = state.get("content_program") or {}
    ready = state.get("program_scaffold_exists") or bool(prog)

    if not ready:
        status = _badge("🔒  Complete Step 2 first", "grey")
        note   = "Set up a drug program before uploading clinical trial data."
    elif "✅" in upload_msg:
        status = _badge("✅  Trial data registered", "green")
        note   = upload_msg.replace("✅ ", "")
    else:
        status = _badge("○  No data registered yet", "grey")
        note   = "Uploading trial data is optional but strongly recommended for accurate content."

    return _step_banner(
        "Step 3 — Clinical Trial Data",
        "Upload your clinical trial CSV. The system maps columns to regulatory placeholders automatically.",
        f"{status}<div style='font-size:0.84em;color:#555;margin-top:8px;'>{note}</div>",
    )


# ── Step 4: Generation status HTML ────────────────────────────────────────────
def _generation_status_html(state: dict, gen_msg: str) -> str:
    prog  = state.get("content_program") or {}
    ready = state.get("program_scaffold_exists") or bool(prog)
    in_prog = state.get("generation_in_progress", False)

    if not ready:
        status = _badge("🔒  Complete Step 2 first", "grey")
        note   = "Set up a drug program before generating content."
    elif in_prog:
        status = _badge("⏳  Generating — this takes 5–15 minutes", "yellow")
        note   = "The AI is writing ICH CTD sections. This page updates automatically."
    elif "✅" in gen_msg or "complete" in gen_msg.lower():
        status = _badge("✅  Generation complete", "green")
        note   = gen_msg
    elif "❌" in gen_msg or "failed" in gen_msg.lower():
        status = _badge("❌  Generation failed", "red")
        note   = gen_msg
    else:
        status = _badge("○  Ready to generate", "blue")
        note   = "Click Generate Documents to start the AI writing pipeline."

    return _step_banner(
        "Step 4 — Generate Documents",
        "AI writes all ICH CTD sections using the trial data and ICH guidelines.",
        f"{status}<div style='font-size:0.84em;color:#555;margin-top:8px;'>{note}</div>",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Action handlers
# ══════════════════════════════════════════════════════════════════════════════

def _action_build_framework(state: dict, log: str):
    sid = state.get("session_id", "default")
    bkt = state.get("bucket", _DEFAULT_BUCKET)
    result = _ctd_post("/extract", {"session_id": sid, "bucket": bkt, "force": False})
    new_state = {**state, **result.get("state_patch", {}), "extraction_in_progress": True}
    new_log   = _log(log, "Started building document framework from ICH M4 guidelines…")
    banner    = _framework_status_html(new_state)
    return new_state, new_log, banner


def _action_publish_framework(state: dict, log: str):
    sid = state.get("session_id", "default")
    bkt = state.get("bucket", _DEFAULT_BUCKET)
    if not state.get("folder_paths"):
        return state, _log(log, "⚠️ Build the framework first."), _framework_status_html(state)
    result = _ctd_post("/approve", {
        "session_id":   sid,
        "bucket":       bkt,
        "folder_paths": state.get("folder_paths", []),
        "state":        state,
    })
    new_state = {**state, **result.get("state_patch", {})}
    msg = result.get("reply", "Framework published.")
    new_log = _log(log, f"✅ {msg}")
    return new_state, new_log, _framework_status_html(new_state)


def _action_setup_program(ta: str, dis: str, drug: str, state: dict, log: str):
    ta   = (ta   or "").strip()
    dis  = (dis  or "").strip()
    drug = (drug or "").strip()
    if not ta or not dis or not drug:
        msg = "⚠️ Please fill in all three fields: Therapeutic Area, Disease, and Drug Name."
        return state, _log(log, msg), _program_status_html(state), msg

    if not (state.get("approved") or state.get("canonical_exists")):
        msg = "⚠️ Complete Step 1 (publish the document framework) first."
        return state, _log(log, msg), _program_status_html(state), msg

    sid = state.get("session_id", "default")
    bkt = state.get("bucket", _DEFAULT_BUCKET)
    result = _ctd_post("/copy", {
        "session_id":       sid,
        "bucket":           bkt,
        "therapeutic_area": ta,
        "disease_type":     dis,
        "drug_name":        drug,
        "state":            state,
    })
    new_state = {**state, **result.get("state_patch", {})}
    msg = result.get("reply", "Program folder created.")
    new_log = _log(log, f"✅ Program set up: {ta} / {dis} / {drug}")
    return new_state, new_log, _program_status_html(new_state), ""


def _action_upload_csv(csv_file, ta: str, dis: str, drug: str, state: dict, log: str):
    ta   = (ta   or "").strip()
    dis  = (dis  or "").strip()
    drug = (drug or "").strip()
    if not ta or not dis or not drug:
        msg = "⚠️ Please fill in the program fields (Therapeutic Area, Disease, Drug Name) in Step 2 first."
        return state, _log(log, msg), _clinical_status_html(msg, state), msg
    if csv_file is None:
        msg = "⚠️ Please select a CSV file."
        return state, _log(log, msg), _clinical_status_html("", state), msg

    bkt = state.get("bucket", _DEFAULT_BUCKET)
    local_path = csv_file if isinstance(csv_file, str) else csv_file.get("path", "")
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
        msg = (
            f"✅ **{data.get('filename', 'File')}** registered — "
            f"{data.get('columns', 0)} columns mapped to regulatory placeholders."
        )
        new_log = _log(log, msg)
        return state, new_log, _clinical_status_html(msg, state), msg
    except Exception as exc:
        msg = f"❌ Upload failed: {exc}"
        return state, _log(log, msg), _clinical_status_html("", state), msg


def _action_generate(ta: str, dis: str, drug: str, state: dict, log: str):
    ta   = (ta   or (state.get("content_program") or {}).get("therapeutic_area", "")).strip()
    dis  = (dis  or (state.get("content_program") or {}).get("disease_type",     "")).strip()
    drug = (drug or (state.get("content_program") or {}).get("drug_name",        "")).strip()

    if not (state.get("program_scaffold_exists") or state.get("content_program")):
        msg = "⚠️ Complete Step 2 (set up drug program) first."
        return state, _log(log, msg), _generation_status_html(state, msg), msg
    if not ta or not dis or not drug:
        msg = "⚠️ Program details are missing. Complete Step 2 first."
        return state, _log(log, msg), _generation_status_html(state, msg), msg

    sid = state.get("session_id", "default")
    bkt = state.get("bucket", _DEFAULT_BUCKET)
    result = _analyst_post("/trigger", {
        "session_id":       sid,
        "bucket":           bkt,
        "therapeutic_area": ta,
        "disease_type":     dis,
        "drug_name":        drug,
        "state":            state,
    })
    new_state = {**state, **result.get("state_patch", {}), "generation_in_progress": True}
    msg = result.get("reply", "Generation started.")
    new_log = _log(log, f"⏳ Generation started for {ta} / {dis} / {drug}…")
    return new_state, new_log, _generation_status_html(new_state, ""), msg


def _action_load_sections(ta: str, dis: str, drug: str, state: dict):
    ta   = (ta   or (state.get("content_program") or {}).get("therapeutic_area", "")).strip()
    dis  = (dis  or (state.get("content_program") or {}).get("disease_type",     "")).strip()
    drug = (drug or (state.get("content_program") or {}).get("drug_name",        "")).strip()

    if not ta or not dis or not drug:
        return gr.update(choices=[], value=None), "⚠️ Enter program details (Therapeutic Area, Disease, Drug Name)."

    bkt  = state.get("bucket", _DEFAULT_BUCKET)
    data = _writer_get("/documents", {
        "therapeutic_area": ta, "disease_type": dis, "drug_name": drug, "bucket_name": bkt,
    })
    docs = data.get("documents", [])
    if not docs:
        return gr.update(choices=[], value=None), "No generated documents found yet. Complete Step 4 first."

    choices = [
        (f"{d['module'].upper()} › {d['section_label']}", f"{d['module']}|{d['section_key']}")
        for d in docs
    ]
    return gr.update(choices=choices, value=None), f"✅ {len(docs)} sections available."


def _action_read_section(selection: str, ta: str, dis: str, drug: str, state: dict) -> str:
    if not selection:
        return ""
    parts = selection.split("|", 1)
    if len(parts) != 2:
        return "⚠️ Invalid selection."
    module, section_key = parts
    ta   = (ta   or (state.get("content_program") or {}).get("therapeutic_area", "")).strip()
    dis  = (dis  or (state.get("content_program") or {}).get("disease_type",     "")).strip()
    drug = (drug or (state.get("content_program") or {}).get("drug_name",        "")).strip()
    bkt  = state.get("bucket", _DEFAULT_BUCKET)
    data = _writer_get("/documents/read", {
        "therapeutic_area": ta, "disease_type": dis, "drug_name": drug,
        "module": module, "section_key": section_key, "bucket_name": bkt,
    })
    return data.get("content", "⚠️ Could not load section.")


# ── Auto-poll ──────────────────────────────────────────────────────────────────
def _poll(state: dict, log: str, gen_msg: str):
    """Poll extraction + generation status every 20 s."""
    st  = state or {}
    sid = st.get("session_id")
    bkt = st.get("bucket", _DEFAULT_BUCKET)
    if not sid:
        return state, log, gen_msg, _framework_status_html(st), _generation_status_html(st, gen_msg)

    prog = st.get("content_program") or {}
    data = _ctd_get("/status", {
        "session_id":       sid,
        "bucket":           bkt,
        "therapeutic_area": prog.get("therapeutic_area") or None,
        "disease_type":     prog.get("disease_type")     or None,
        "drug_name":        prog.get("drug_name")        or None,
    })
    ext  = data.get("extraction") or {}
    cont = data.get("content")    or {}

    new_state = dict(st)
    new_log   = log
    new_gen   = gen_msg

    # ── Extraction done ───────────────────────────────────────────────────────
    if st.get("extraction_in_progress") and ext.get("status") == "done":
        sess  = _ctd_get("/session", {"session_id": sid, "bucket": bkt})
        paths = sess.get("folder_paths", [])
        if paths:
            new_state = {**new_state, "folder_paths": paths, "extraction_in_progress": False}
            if sess.get("ctd_output"):
                new_state["ctd_output"] = sess["ctd_output"]
            n = len(paths)
            new_log = _log(new_log, f"✅ Framework built — {n} folders. Review and click Publish Framework.")

    # ── Content done / failed ─────────────────────────────────────────────────
    if st.get("generation_in_progress") and cont:
        cstatus = cont.get("status")
        if cstatus == "done":
            secs     = cont.get("sections_written", "?")
            val_pass = cont.get("validation_passed", False)
            val_sum  = cont.get("validation_summary", "") or ("passed" if val_pass else "issues found")
            new_state = {**new_state, "generation_in_progress": False, "content_run_id": None}
            new_gen   = f"✅ Complete — {secs} sections written. Validation: {val_sum}"
            new_log   = _log(new_log, new_gen)
        elif cstatus == "failed":
            new_state = {**new_state, "generation_in_progress": False, "content_run_id": None}
            new_gen   = f"❌ Failed — {cont.get('error', 'unknown error')}"
            new_log   = _log(new_log, new_gen)

    return (
        new_state,
        new_log,
        new_gen,
        _framework_status_html(new_state),
        _generation_status_html(new_state, new_gen),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Gradio UI
# ══════════════════════════════════════════════════════════════════════════════

_CSS = """
footer { display: none !important; }
/* Force light mode */
:root, html.dark {
    --body-background-fill:      #f0f4f8 !important;
    --background-fill-primary:   #ffffff !important;
    --background-fill-secondary: #f8fafc !important;
    --body-text-color:           #1a1a2e !important;
    --block-background-fill:     #ffffff !important;
    --block-border-color:        #e2e8f0 !important;
    --input-background-fill:     #ffffff !important;
    --block-label-text-color:    #1a1a2e !important;
    --block-title-text-color:    #1a1a2e !important;
}
body, .gradio-container { background-color: #f0f4f8 !important; }
html.dark .gradio-container, html.dark body { background-color: #f0f4f8 !important; color: #1a1a2e !important; }
html.dark .block, html.dark .panel, html.dark fieldset { background-color: #ffffff !important; }
html.dark input, html.dark textarea, html.dark select { background-color: #ffffff !important; color: #1a1a2e !important; }

/* Header */
.pro-header { background: linear-gradient(135deg, #1a4f8a 0%, #0f3460 100%);
              border-radius: 10px; padding: 24px 28px; margin-bottom: 20px; }
.pro-header h1 { color: #ffffff !important; font-size: 1.5em; font-weight: 700; margin: 0 0 4px 0; }
.pro-header p  { color: #b3c9e8 !important; font-size: 0.88em; margin: 0; }

/* Progress bar */
.progress-bar { display: flex; gap: 0; margin-bottom: 24px; border-radius: 8px; overflow: hidden; }
.progress-step { flex: 1; padding: 10px 6px; text-align: center; font-size: 0.78em;
                 font-weight: 600; background: #e2e8f0; color: #64748b; }
.progress-step.done  { background: #d1fae5; color: #065f46; }
.progress-step.active { background: #1a4f8a; color: #ffffff; }

/* Tab styling */
.tab-nav button { font-size: 0.85em !important; font-weight: 600 !important; }
.tab-nav button.selected { border-bottom: 3px solid #1a4f8a !important; color: #1a4f8a !important; }

/* Activity log */
.activity-log textarea { font-family: 'Courier New', monospace !important;
                          font-size: 0.80em !important; background: #1e2235 !important;
                          color: #a3e4b0 !important; border-radius: 6px !important; }

/* Action buttons */
.action-btn { min-width: 180px !important; }

/* Section content viewer */
.section-viewer { border: 1px solid #e2e8f0 !important; border-radius: 8px !important;
                  padding: 20px !important; background: #ffffff !important; }
"""

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
    body_background_fill="#f0f4f8",
    background_fill_primary="white",
    background_fill_secondary="#f8fafc",
    body_text_color="#1a1a2e",
    block_background_fill="white",
    input_background_fill="white",
)


# ── Build Gradio app ───────────────────────────────────────────────────────────
with gr.Blocks(title="Regulatory Authoring Platform", css=_CSS, theme=_theme, head=_HEAD) as demo:

    # ── Persistent state ───────────────────────────────────────────────────────
    _state           = gr.State(value={})
    _browser_session = gr.BrowserState("")
    _gen_msg         = gr.State(value="")      # last generation status text
    _upload_msg      = gr.State(value="")      # last upload status text

    # ── Header ─────────────────────────────────────────────────────────────────
    gr.HTML("""
    <div class="pro-header">
      <h1>⚕  Regulatory Authoring Platform</h1>
      <p>AI-assisted authoring for ICH M4(R4) Common Technical Documents &mdash;
         follow the steps below to generate your regulatory submission content.</p>
    </div>
    """)

    # ── Progress bar (visual only — updated via poll) ──────────────────────────
    _progress_html = gr.HTML("""
    <div class="progress-bar">
      <div class="progress-step">1 · Document Framework</div>
      <div class="progress-step">2 · Drug Program</div>
      <div class="progress-step">3 · Trial Data</div>
      <div class="progress-step">4 · Generate</div>
      <div class="progress-step">5 · Review</div>
    </div>
    """)

    # ── Main tabs ──────────────────────────────────────────────────────────────
    with gr.Tabs(elem_classes=["tab-nav"]):

        # ── STEP 1: Document Framework ─────────────────────────────────────────
        with gr.Tab("1 · Document Framework"):
            _s1_banner = gr.HTML(
                _step_banner(
                    "Step 1 — Document Framework",
                    "Build and publish the ICH M4(R4) folder hierarchy. Done once; shared across all programs.",
                    _badge("○  Not started yet", "grey"),
                )
            )
            gr.HTML(_info_box(
                "This step creates the official ICH M4(R4) Common Technical Document (CTD) folder "
                "structure by querying the ICH guidelines. It takes 3–8 minutes and only needs to "
                "be done once — the result is shared across all drug programs."
            ))
            with gr.Row():
                _s1_build_btn   = gr.Button("🏗  Build Document Framework",  variant="primary",   elem_classes=["action-btn"])
                _s1_publish_btn = gr.Button("✅  Publish Framework",          variant="secondary", elem_classes=["action-btn"])
            _s1_msg = gr.Markdown(value="")

        # ── STEP 2: Drug Program ───────────────────────────────────────────────
        with gr.Tab("2 · Drug Program"):
            _s2_banner = gr.HTML(
                _step_banner(
                    "Step 2 — Drug Program",
                    "Create a dedicated folder for all regulatory documents for this drug and indication.",
                    _badge("🔒  Complete Step 1 first", "grey"),
                )
            )
            gr.HTML(_info_box(
                "Provide the details of the drug program you are authoring for. "
                "This creates a structured GCS folder that holds all generated CTD documents, "
                "clinical data, and status files for this specific drug and indication."
            ))
            with gr.Row():
                _s2_ta   = gr.Textbox(label="Therapeutic Area",  placeholder="e.g. Neurology",    scale=1)
                _s2_dis  = gr.Textbox(label="Disease / Indication", placeholder="e.g. Bell's Palsy", scale=1)
                _s2_drug = gr.Textbox(label="Drug Name",          placeholder="e.g. Prednisolone", scale=1)
            _s2_setup_btn = gr.Button("📁  Set Up Program Folder", variant="primary", elem_classes=["action-btn"])
            _s2_msg = gr.Markdown(value="")

        # ── STEP 3: Clinical Trial Data ────────────────────────────────────────
        with gr.Tab("3 · Trial Data"):
            _s3_banner = gr.HTML(
                _step_banner(
                    "Step 3 — Clinical Trial Data",
                    "Upload your clinical trial results CSV. The AI maps columns to regulatory placeholders.",
                    _badge("🔒  Complete Step 2 first", "grey"),
                )
            )
            gr.HTML(_info_box(
                "Upload a CSV file containing your clinical trial data. The system will automatically "
                "identify relevant columns (e.g. recovery rates, demographics, adverse events) and "
                "connect them to the correct placeholders in the regulatory document templates. "
                "This step is optional but strongly recommended for accurate content."
            ))
            _s3_file = gr.File(label="Clinical trial CSV file", file_types=[".csv"])
            with gr.Row():
                _s3_ta_disp   = gr.Textbox(label="Therapeutic Area",    interactive=False, scale=1)
                _s3_dis_disp  = gr.Textbox(label="Disease / Indication", interactive=False, scale=1)
                _s3_drug_disp = gr.Textbox(label="Drug Name",            interactive=False, scale=1)
            _s3_upload_btn = gr.Button("⬆  Register Trial Data", variant="primary", elem_classes=["action-btn"])
            _s3_msg = gr.Markdown(value="")

        # ── STEP 4: Generate Documents ─────────────────────────────────────────
        with gr.Tab("4 · Generate"):
            _s4_banner = gr.HTML(
                _step_banner(
                    "Step 4 — Generate Documents",
                    "AI writes all ICH CTD sections using the trial data and ICH guidelines.",
                    _badge("🔒  Complete Step 2 first", "grey"),
                )
            )
            gr.HTML(_info_box(
                "Click Generate Documents to start the AI writing pipeline. The system will: "
                "① Resolve all placeholders using your trial data, "
                "② Generate each ICH CTD section (Module 2 + Module 5), "
                "③ Run cross-module consistency validation. "
                "This takes 5–15 minutes. You will see a live status update on this page."
            ))
            with gr.Row():
                _s4_ta_disp   = gr.Textbox(label="Therapeutic Area",    interactive=False, scale=1)
                _s4_dis_disp  = gr.Textbox(label="Disease / Indication", interactive=False, scale=1)
                _s4_drug_disp = gr.Textbox(label="Drug Name",            interactive=False, scale=1)
            _s4_gen_btn = gr.Button("🚀  Generate Documents", variant="primary", elem_classes=["action-btn"])
            _s4_msg = gr.Markdown(value="")

        # ── STEP 5: Review & Export ────────────────────────────────────────────
        with gr.Tab("5 · Review"):
            gr.HTML(_step_banner(
                "Step 5 — Review Generated Documents",
                "Browse and read every generated CTD section directly in the browser.",
                _badge("Browse your generated regulatory content", "blue"),
            ))
            gr.HTML(_info_box(
                "Select a section from the dropdown to read its full content. "
                "Documents are grouped by module (Module 2 — Quality & Clinical Overview, "
                "Module 5 — Clinical Study Reports). Click Load Sections if the list is empty."
            ))
            with gr.Row():
                _s5_ta_disp   = gr.Textbox(label="Therapeutic Area",    interactive=False, scale=1)
                _s5_dis_disp  = gr.Textbox(label="Disease / Indication", interactive=False, scale=1)
                _s5_drug_disp = gr.Textbox(label="Drug Name",            interactive=False, scale=1)
            with gr.Row():
                _s5_load_btn = gr.Button("🔄  Load Sections", variant="secondary", scale=1)
                _s5_load_msg = gr.Markdown(value="", scale=3)
            _s5_dropdown = gr.Dropdown(
                choices=[], label="Select a CTD section to read", interactive=True,
            )
            _s5_content = gr.Markdown(
                value="", label="", elem_classes=["section-viewer"],
            )

    # ── Activity Log (always visible at bottom) ────────────────────────────────
    with gr.Accordion("📋  Activity Log", open=False):
        _log_box = gr.Textbox(
            value="", label="", lines=8, interactive=False,
            elem_classes=["activity-log"],
            placeholder="Actions and status updates will appear here…",
        )

    # ── Hidden config ──────────────────────────────────────────────────────────
    with gr.Accordion("⚙  Advanced Configuration", open=False):
        _bucket_input = gr.Textbox(label="GCS Document Bucket", value=_DEFAULT_BUCKET)

    # ══════════════════════════════════════════════════════════════════════════
    # Page load
    # ══════════════════════════════════════════════════════════════════════════
    def _on_load(bucket: str, browser_session: str):
        state, sid = _init_session(bucket or _DEFAULT_BUCKET, browser_session)
        prog = state.get("content_program") or {}
        ta   = prog.get("therapeutic_area", "")
        dis  = prog.get("disease_type",     "")
        drug = prog.get("drug_name",        "")
        log  = f"[{_ts()}]  Session loaded — ID: {sid[:8]}…"
        s1b  = _framework_status_html(state)
        s2b  = _program_status_html(state)
        return (
            state, sid, log,
            s1b, s2b,
            gr.update(value=ta), gr.update(value=dis), gr.update(value=drug),  # step 2
            gr.update(value=ta), gr.update(value=dis), gr.update(value=drug),  # step 3
            gr.update(value=ta), gr.update(value=dis), gr.update(value=drug),  # step 4
            gr.update(value=ta), gr.update(value=dis), gr.update(value=drug),  # step 5
        )

    demo.load(
        fn=_on_load,
        inputs=[_bucket_input, _browser_session],
        outputs=[
            _state, _browser_session, _log_box,
            _s1_banner, _s2_banner,
            _s2_ta, _s2_dis, _s2_drug,
            _s3_ta_disp, _s3_dis_disp, _s3_drug_disp,
            _s4_ta_disp, _s4_dis_disp, _s4_drug_disp,
            _s5_ta_disp, _s5_dis_disp, _s5_drug_disp,
        ],
    )

    # ══════════════════════════════════════════════════════════════════════════
    # Button wiring
    # ══════════════════════════════════════════════════════════════════════════

    # Step 1 — Build
    _s1_build_btn.click(
        fn=_action_build_framework,
        inputs=[_state, _log_box],
        outputs=[_state, _log_box, _s1_banner],
    )

    # Step 1 — Publish
    _s1_publish_btn.click(
        fn=_action_publish_framework,
        inputs=[_state, _log_box],
        outputs=[_state, _log_box, _s1_banner],
    )

    # Step 2 — Setup program (also propagates program to steps 3-5)
    def _setup_and_propagate(ta, dis, drug, state, log):
        new_state, new_log, s2b, msg = _action_setup_program(ta, dis, drug, state, log)
        prog = new_state.get("content_program") or {}
        t = prog.get("therapeutic_area", ta)
        d = prog.get("disease_type",     dis)
        n = prog.get("drug_name",        drug)
        return (
            new_state, new_log, s2b, msg,
            gr.update(value=t), gr.update(value=d), gr.update(value=n),
            gr.update(value=t), gr.update(value=d), gr.update(value=n),
            gr.update(value=t), gr.update(value=d), gr.update(value=n),
        )

    _s2_setup_btn.click(
        fn=_setup_and_propagate,
        inputs=[_s2_ta, _s2_dis, _s2_drug, _state, _log_box],
        outputs=[
            _state, _log_box, _s2_banner, _s2_msg,
            _s3_ta_disp, _s3_dis_disp, _s3_drug_disp,
            _s4_ta_disp, _s4_dis_disp, _s4_drug_disp,
            _s5_ta_disp, _s5_dis_disp, _s5_drug_disp,
        ],
    )

    # Step 3 — Upload CSV
    def _upload_and_banner(csv_file, state, log, upload_msg):
        prog = state.get("content_program") or {}
        ta   = prog.get("therapeutic_area", "")
        dis  = prog.get("disease_type",     "")
        drug = prog.get("drug_name",        "")
        new_state, new_log, banner, msg = _action_upload_csv(csv_file, ta, dis, drug, state, log)
        return new_state, new_log, banner, msg, msg

    _s3_upload_btn.click(
        fn=_upload_and_banner,
        inputs=[_s3_file, _state, _log_box, _upload_msg],
        outputs=[_state, _log_box, _s3_banner, _s3_msg, _upload_msg],
    )

    # Step 4 — Generate
    def _generate_action(state, log, gen_msg):
        prog = state.get("content_program") or {}
        ta   = prog.get("therapeutic_area", "")
        dis  = prog.get("disease_type",     "")
        drug = prog.get("drug_name",        "")
        new_state, new_log, banner, msg = _action_generate(ta, dis, drug, state, log)
        return new_state, new_log, banner, msg, msg

    _s4_gen_btn.click(
        fn=_generate_action,
        inputs=[_state, _log_box, _gen_msg],
        outputs=[_state, _log_box, _s4_banner, _s4_msg, _gen_msg],
    )

    # Step 5 — Load sections
    def _load_sections_action(state):
        prog = state.get("content_program") or {}
        ta   = prog.get("therapeutic_area", "")
        dis  = prog.get("disease_type",     "")
        drug = prog.get("drug_name",        "")
        return _action_load_sections(ta, dis, drug, state)

    _s5_load_btn.click(
        fn=_load_sections_action,
        inputs=[_state],
        outputs=[_s5_dropdown, _s5_load_msg],
    )

    # Step 5 — Read section
    def _read_section_action(selection, state):
        prog = state.get("content_program") or {}
        ta   = prog.get("therapeutic_area", "")
        dis  = prog.get("disease_type",     "")
        drug = prog.get("drug_name",        "")
        return _action_read_section(selection, ta, dis, drug, state)

    _s5_dropdown.change(
        fn=_read_section_action,
        inputs=[_s5_dropdown, _state],
        outputs=[_s5_content],
    )

    # ── Auto-poll every 20 s ───────────────────────────────────────────────────
    _timer = gr.Timer(value=20, active=True)
    _timer.tick(
        fn=_poll,
        inputs=[_state, _log_box, _gen_msg],
        outputs=[_state, _log_box, _gen_msg, _s1_banner, _s4_banner],
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port, show_api=False)
