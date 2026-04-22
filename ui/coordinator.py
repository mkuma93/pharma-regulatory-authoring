"""
ui/coordinator.py

Global UI coordinator LangGraph pipeline.

This is the top-level intent router for the Gradio UI (ui/app.py).
It handles ALL user-facing intents and dispatches to the correct
downstream service (CTD structure, ICH4 writer, clinical analyst, etc.).

Architecture
────────────
Two-node LangGraph:

  understand → decide

  understand  — GPT-4o-mini reads the user message + full workflow context and
                extracts what the user wants (intent + program fields + feedback).

  decide      — GPT-4o-mini reasons about the current workflow state, checks
                prerequisites, and produces ONE of:
                   • clarify  — a question or guidance message to send back to
                                the user before any action can proceed.
                   • proceed  — all prerequisites met, action can execute now.

Intents handled
───────────────
  extract           — build/load the canonical ICH CTD folder tree
  approve           — accept the currently shown structure
  disapprove        — reject or request changes to the structure
  copy              — scaffold CTD folder tree for a specific drug program in GCS
  write             — run the full CTD content generation pipeline for a program
  status            — ask what is currently loaded or running
  rewrite_section   — regenerate one or more specific CTD sections
  analyze_data      — analyse clinical trial data / CSV for a program
  help              — anything else / unclear

Public surface (used by ui/app.py)
───────────────────────────────────
  CoordinatorDecision  — output model
  run_coordinator(message, session_state) → CoordinatorDecision
"""
from __future__ import annotations

import os
import re
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


# ── Output models ─────────────────────────────────────────────────────────────

class IntentResult(BaseModel):
    """
    Raw intent + entity extraction (understand node).
    Kept as a separate model so the decide node receives structured input.
    """
    intent: Literal["extract", "approve", "disapprove", "copy", "write", "status",
                    "rewrite_section", "analyze_data", "help"] = Field(
        description=(
            "The user's intent:\n"
            "  extract           — build/load the canonical ICH CTD folder tree\n"
            "  approve           — accept the currently shown structure\n"
            "  disapprove        — reject or request changes to the structure\n"
            "  copy              — scaffold the CTD structure for a specific drug program in GCS\n"
            "  write             — generate / fill CTD section content (full pipeline)\n"
            "  status            — ask what is currently loaded or running\n"
            "  rewrite_section   — regenerate one or more specific CTD sections\n"
            "  analyze_data      — analyse clinical trial data / CSV for a program\n"
            "  help              — anything else / unclear"
        )
    )
    force_reextract: bool = Field(
        default=False,
        description="True when the user explicitly wants to rebuild/refresh.",
    )
    therapeutic_area: str | None = Field(
        default=None,
        description="Therapeutic area mentioned (e.g. oncology, neurology).",
    )
    disease_type: str | None = Field(
        default=None,
        description="Disease or indication mentioned (e.g. lung cancer, bells palsy).",
    )
    drug_name: str | None = Field(
        default=None,
        description="Drug or compound name mentioned (e.g. carboplatin, prednisolone).",
    )
    feedback: str | None = Field(
        default=None,
        description="For disapprove intent only: concise summary of the user's concern.",
    )
    section_keys: list[str] = Field(
        default_factory=list,
        description=(
            "For rewrite_section only: list of CTD section keys to rewrite, "
            "e.g. ['2.5_clinical_overview', '5.3_clinical_study_reports']. "
            "Empty list means all sections."
        ),
    )


class CoordinatorDecision(BaseModel):
    """
    Final coordinator output consumed by ui/app.py.

    outcome == 'clarify'
        reply is sent directly to the user; no action handler is called.

    outcome == 'proceed'
        All prerequisites are met; app.py dispatches to the matching
        action handler.
    """
    outcome: Literal["clarify", "proceed"] = Field(
        description="'clarify' — ask user something; 'proceed' — run the action."
    )
    # ── resolved intent (always set) ─────────────────────────────────────────
    intent: Literal["extract", "approve", "disapprove", "copy", "write", "status",
                    "rewrite_section", "analyze_data", "help"] = Field(
        description="The resolved intent to execute when outcome is 'proceed'."
    )
    # ── resolved program fields ───────────────────────────────────────────────
    therapeutic_area: str | None = Field(default=None)
    disease_type: str | None = Field(default=None)
    drug_name: str | None = Field(default=None)
    # ── other extracted fields ────────────────────────────────────────────────
    force_reextract: bool = Field(default=False)
    feedback: str | None = Field(default=None)
    section_keys: list[str] = Field(default_factory=list)
    # ── coordinator reply (set when outcome == 'clarify') ─────────────────────
    reply: str | None = Field(
        default=None,
        description="Markdown message to display to the user when outcome is 'clarify'.",
    )


# ── LangGraph state ───────────────────────────────────────────────────────────

class _CoordState(TypedDict):
    message: str
    workflow_context: str   # serialised workflow state passed in from app.py
    intent_result: IntentResult | None
    decision: CoordinatorDecision | None


# ── System prompts ────────────────────────────────────────────────────────────

_UNDERSTAND_SYSTEM = """\
You are the understanding layer of an ICH M4(R4) CTD (Common Technical Document)
regulatory assistant. Your only job is to extract what the user wants.

Classify the message into one intent:
  extract           — build/load the canonical ICH CTD folder tree from the ICH index
  approve           — accept the currently shown CTD structure
  disapprove        — reject or request changes to the structure
  copy              — scaffold the CTD folder structure for a specific drug program in GCS
                      e.g. "copy to neurology/bells palsy/prednisolone",
                           "set up authoring for oncology / lung cancer / carboplatin",
                           "scaffold a program for cardiology / hypertension / lisinopril",
                           "create program for neurology/bells palsy/prednisolone",
                           "set up neurology / bells palsy / prednisolone"
  write             — run the full CTD content generation pipeline for a program
                      e.g. "generate content", "generate CTD section content for ...",
                           "write the sections for ..."
  status            — ask what is currently loaded or running in the background
  rewrite_section   — regenerate one or more specific CTD section documents
                      e.g. "rewrite section 2.5", "redo the clinical overview"
  analyze_data      — analyse clinical trial data / uploaded CSV for a program
                      e.g. "analyse the clinical data", "summarise trial results",
                           "what does the data show for prednisolone?"
  help              — unclear or out of scope

Also extract (when present in the message):
  force_reextract     — user explicitly wants a fresh rebuild (re-extract, refresh, rebuild)
  therapeutic_area    — e.g. oncology, neurology, cardiology
  disease_type        — e.g. lung cancer, bells palsy, hypertension
  drug_name           — e.g. carboplatin, prednisolone, lisinopril
  feedback            — for disapprove only: one-sentence summary of the concern
  section_keys        — for rewrite_section only: list of CTD section keys,
                        e.g. ['2.5_clinical_overview']

IMPORTANT: messages often use a slash-separated triplet, with or without spaces around slashes:
  "<therapeutic_area> / <disease_type> / <drug_name>"  (spaces around slashes)
  "<therapeutic_area>/<disease_type>/<drug_name>"       (no spaces)
  e.g. "oncology / lung cancer / carboplatin"  → ta=oncology, disease=lung cancer, drug=carboplatin
  e.g. "neurology/bells palsy/prednisolone"    → ta=neurology, disease=bells palsy, drug=prednisolone
  Always split on "/" and trim whitespace from each part to extract all three fields.
  The word before the triplet (e.g. "copy to", "set up authoring for") is NOT part of the fields.
"""

_DECIDE_SYSTEM = """\
You are the decision layer of an ICH M4(R4) CTD regulatory assistant.

You receive:
  1. What the user wants (intent + extracted fields from the understand step).
  2. The current workflow state.

Your job is to decide ONE of:
  proceed  — all prerequisites are met, the action can run now.
  clarify  — something is missing or a prerequisite has not been completed;
             compose a helpful Markdown reply to guide the user.

--- Workflow prerequisites ---

extract:
  No prerequisites. Always proceed (unless force_reextract=false and a structure
  already exists — in that case proceed anyway, the handler will show the cached one).

approve:
  PREREQUISITE: paths_loaded > 0.
  RULE: if paths_loaded > 0 → ALWAYS outcome=proceed.
  If paths_loaded=0 → clarify: ask the user to extract the ICH structure first.
  IMPORTANT: approval CREATES the canonical default; canonical_exists=false is normal.

disapprove:
  PREREQUISITE: paths_loaded > 0.
  If paths_loaded=0 → clarify: ask the user to extract first.

copy (scaffold a program directory):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed.
  If any program field missing → clarify: ask only for the missing fields, warmly.

write (generate content):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed.
  NOTE: approval is NOT required. Scaffolding will be done automatically if needed.
  If program fields missing → clarify: ask for the missing fields, warmly.

rewrite_section (regenerate specific sections):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed.
  NOTE: section_keys is optional — empty means rewrite all sections.
  If program fields missing → clarify.

analyze_data (clinical data analysis):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed.
  NOTE: the analyst will read clinical CSV from GCS for the given program.
  If program fields missing → clarify.

status / help:
  Always proceed.

--- Reply style for clarify ---
- Be conversational and warm, not robotic.
- Use bullet points or a numbered list when showing multiple steps.
- If asking for missing fields, ask for all missing ones in a single message.
- Never repeat information the user already provided.
- Example for missing fields:
    "Got it — to generate content I just need a couple more details:
    - **Disease / indication**: e.g. *lung cancer*, *bells palsy*
    - **Drug / compound**: e.g. *carboplatin*, *prednisolone*"
"""


# ── LangGraph nodes ───────────────────────────────────────────────────────────

def _understand_node(state: _CoordState) -> _CoordState:
    """Extract intent and entities from the user message."""
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    result: IntentResult = llm.with_structured_output(IntentResult).invoke([
        SystemMessage(content=_UNDERSTAND_SYSTEM),
        HumanMessage(content=(
            f"Session context: {state['workflow_context']}\n\n"
            f"User message: {state['message']}"
        )),
    ])
    return {"intent_result": result}


def _decide_node(state: _CoordState) -> _CoordState:
    """Reason about prerequisites and either clarify or proceed."""
    ir: IntentResult = state["intent_result"]
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    decision: CoordinatorDecision = llm.with_structured_output(CoordinatorDecision).invoke([
        SystemMessage(content=_DECIDE_SYSTEM),
        HumanMessage(content=(
            f"Workflow state: {state['workflow_context']}\n\n"
            f"Understood intent: {ir.model_dump_json()}\n\n"
            "What is your decision (clarify or proceed)?"
        )),
    ])
    # Hard guardrail: approve/disapprove with paths loaded must always proceed.
    _m = re.search(r"paths_loaded=(\d+)", state.get("workflow_context", ""))
    if (decision.outcome == "clarify"
            and ir.intent in ("approve", "disapprove")
            and _m and int(_m.group(1)) > 0):
        decision = CoordinatorDecision(outcome="proceed", intent=ir.intent)

    # Hard guardrail: actions requiring program fields with all 3 present must
    # always proceed — the action handler does the real GCS check at runtime.
    _has_all_fields = bool(ir.therapeutic_area and ir.disease_type and ir.drug_name)
    if (decision.outcome == "clarify"
            and ir.intent in ("copy", "write", "rewrite_section", "analyze_data")
            and _has_all_fields):
        decision = CoordinatorDecision(
            outcome="proceed", intent=ir.intent,
            therapeutic_area=ir.therapeutic_area,
            disease_type=ir.disease_type,
            drug_name=ir.drug_name,
        )

    # Always propagate the extracted fields so app.py can use them even if
    # the LLM forgot to copy them into the CoordinatorDecision.
    if decision.therapeutic_area is None and ir.therapeutic_area:
        decision.therapeutic_area = ir.therapeutic_area
    if decision.disease_type is None and ir.disease_type:
        decision.disease_type = ir.disease_type
    if decision.drug_name is None and ir.drug_name:
        decision.drug_name = ir.drug_name
    if not decision.force_reextract:
        decision.force_reextract = ir.force_reextract
    if decision.feedback is None and ir.feedback:
        decision.feedback = ir.feedback
    if not decision.section_keys and ir.section_keys:
        decision.section_keys = ir.section_keys
    return {"decision": decision}


# ── Build and compile the graph ───────────────────────────────────────────────

_graph = StateGraph(_CoordState)
_graph.add_node("understand", _understand_node)
_graph.add_node("decide",     _decide_node)
_graph.set_entry_point("understand")
_graph.add_edge("understand", "decide")
_graph.add_edge("decide", END)

coordinator_app = _graph.compile()


# ── Keyword-based quick classifier (no LLM needed for obvious intents) ────────

_SLASH_RE = re.compile(r'(.+?)\s*/\s*(.+?)\s*/\s*(.+)')

_LEADING_ACTIONS = (
    "copy to", "set up authoring for", "set up for", "set up",
    "scaffold for", "scaffold a program for", "scaffold",
    "create program for", "create a program for",
    "generate content for", "write sections for",
    "analyse data for", "analyze data for",
    "analyse the data for", "analyze the data for",
)


def _extract_triplet(msg_lower: str, original: str):
    """Try to extract ta/disease/drug from a slash-separated triplet."""
    m = _SLASH_RE.search(original)
    if not m:
        m = _SLASH_RE.search(msg_lower)
    if not m:
        return None, None, None
    ta   = m.group(1).strip()
    dis  = m.group(2).strip()
    drug = m.group(3).strip()
    # Strip leading action words from ta (longest match first)
    ta_lc = ta.lower()
    for prefix in sorted(_LEADING_ACTIONS, key=len, reverse=True):
        if ta_lc.startswith(prefix):
            ta = ta[len(prefix):].strip()
            break
    else:
        # Aggressive fallback: strip everything up to and including the last "for "
        # handles cases like "generate ctd section content for neurology"
        _for_m = re.search(r'\bfor\s+(.+)$', ta, re.IGNORECASE)
        if _for_m:
            ta = _for_m.group(1).strip()
    return ta or None, dis or None, drug or None


def _keyword_classify(message: str) -> CoordinatorDecision | None:
    """
    Fast, deterministic classifier for obvious intents.
    Returns None when intent is ambiguous (fall through to LLM).
    """
    msg = message.strip()
    ml  = msg.lower()

    ta, dis, drug = _extract_triplet(ml, msg)

    # ── copy / scaffold ───────────────────────────────────────────────────────
    _COPY_KW = ("copy to ", "copy ", "scaffold", "set up authoring", "set up for ",
                "set up ", "create program", "setup authoring", "setup for ")
    if any(ml.startswith(kw) for kw in _COPY_KW) or (ta and dis and drug and (
            "copy" in ml or "scaffold" in ml or "set up" in ml or "setup" in ml)):
        return CoordinatorDecision(
            outcome="proceed", intent="copy",
            therapeutic_area=ta, disease_type=dis, drug_name=drug,
        )

    # ── write / generate content ──────────────────────────────────────────────
    _WRITE_KW = ("generate content", "generate ctd", "write sections", "write the sections",
                 "write section", "generate section", "run the pipeline", "generate documents")
    if any(kw in ml for kw in _WRITE_KW):
        return CoordinatorDecision(
            outcome="proceed", intent="write",
            therapeutic_area=ta, disease_type=dis, drug_name=drug,
        )

    # ── extract / build structure ─────────────────────────────────────────────
    _EXTRACT_KW = ("extract", "build the ich", "build ich", "re-extract",
                   "reextract", "rebuild the", "build ctd", "generate the ich",
                   "load the ich", "fetch the ich")
    if any(kw in ml for kw in _EXTRACT_KW):
        return CoordinatorDecision(outcome="proceed", intent="extract")

    # ── approve ───────────────────────────────────────────────────────────────
    if ml.strip() in ("approve", "yes approve", "approve it", "approve this",
                       "approve and publish", "looks good", "publish it"):
        return CoordinatorDecision(outcome="proceed", intent="approve")
    if ml.startswith("approve"):
        return CoordinatorDecision(outcome="proceed", intent="approve")

    # ── disapprove ────────────────────────────────────────────────────────────
    if ml.strip() in ("disapprove", "reject", "reject this") or ml.startswith("disapprove"):
        return CoordinatorDecision(outcome="proceed", intent="disapprove")

    # ── rewrite section ───────────────────────────────────────────────────────
    _REWRITE_KW = ("rewrite section", "redo section", "regenerate section",
                   "rewrite the section", "redo the section", "update section")
    if any(kw in ml for kw in _REWRITE_KW):
        return CoordinatorDecision(
            outcome="proceed", intent="rewrite_section",
            therapeutic_area=ta, disease_type=dis, drug_name=drug,
        )

    # ── analyze clinical data ─────────────────────────────────────────────────
    _ANALYZE_KW = ("analyse the clinical", "analyze the clinical", "analyse clinical",
                   "analyze clinical", "analyse the data", "analyze the data",
                   "what does the data", "what does the trial", "summarise trial",
                   "summarize trial", "clinical data for", "clinical analysis")
    if any(kw in ml for kw in _ANALYZE_KW):
        return CoordinatorDecision(
            outcome="proceed", intent="analyze_data",
            therapeutic_area=ta, disease_type=dis, drug_name=drug,
        )

    # ── status ────────────────────────────────────────────────────────────────
    _STATUS_KW = ("what is", "what's", "status", "what have you", "what are you",
                  "what is running", "whats running", "current")
    if any(ml.startswith(kw) for kw in _STATUS_KW) or "status" in ml:
        return CoordinatorDecision(outcome="proceed", intent="status")

    return None  # ambiguous — fall through to LLM


# ── Public helper ─────────────────────────────────────────────────────────────

def run_coordinator(message: str, session_state: dict) -> CoordinatorDecision:
    """
    Run the coordinator: try fast keyword classifier first, then LLM graph.

    Returns a CoordinatorDecision.  Falls back to a 'proceed/help' decision
    on any LLM error so the UI never hard-fails.
    """
    import sys

    st = session_state or {}
    paths    = st.get("folder_paths", [])
    approved = st.get("approved", False)
    prog     = st.get("content_program") or {}

    canonical_exists = st.get("canonical_exists", False)
    workflow_context = (
        f"paths_loaded={len(paths)}, "
        f"canonical_exists={canonical_exists}, "
        f"approved={approved}, "
        f"bucket={st.get('bucket', 'unknown')}, "
        f"program_scaffold_exists={st.get('program_scaffold_exists', False)}, "
        f"content_run_id={'set' if st.get('content_run_id') else 'none'}, "
        f"current_program={prog or 'none'}, "
        f"awaiting_feedback={st.get('awaiting_feedback', False)}"
    )

    # ── Fast path: keyword classifier ─────────────────────────────────────────
    quick = _keyword_classify(message)
    if quick is not None:
        print(f"[coordinator] keyword  msg={message!r:<60} → intent={quick.intent}  ta={quick.therapeutic_area}  dis={quick.disease_type}  drug={quick.drug_name}", flush=True)
        sys.stdout.flush()
        return quick

    # ── Slow path: LLM graph ───────────────────────────────────────────────────
    try:
        out = coordinator_app.invoke({
            "message":          message,
            "workflow_context": workflow_context,
            "intent_result":    None,
            "decision":         None,
        })
        decision = out["decision"]
        print(f"[coordinator] llm      msg={message!r:<60} → intent={decision.intent}  outcome={decision.outcome}", flush=True)
        sys.stdout.flush()
        return decision
    except Exception as exc:
        import traceback
        print(f"[coordinator] LLM FAILED for msg={message!r}: {exc}", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        return CoordinatorDecision(outcome="proceed", intent="help")
