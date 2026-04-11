"""
ctd_structure/deploy/main.py

Full-coordinator LangGraph pipeline for the CTD Structure assistant.

Architecture
────────────
The coordinator is a two-node LangGraph:

  understand → decide

  understand  — GPT-4o-mini reads the user message + full workflow context and
                extracts what the user wants (intent + program fields + feedback).

  decide      — GPT-4o-mini reasons about the current workflow state, checks
                prerequisites, identifies any missing information, and produces
                ONE of:
                   • clarify   — a question or guidance message to send back to
                                 the user before any action can proceed.
                   • proceed   — all prerequisites met, action can execute now.

Public surface (used by app.py)
────────────────────────────────
  CoordinatorDecision  — output model (replaces IntentResult)
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
                      "generate_template", "rewrite_section", "analyze_data", "help"] = Field(
        description=(
            "The user's intent:\n"
            "  extract           — build/load the canonical ICH CTD folder tree\n"
            "  approve           — accept the currently shown structure\n"
            "  disapprove        — reject or request changes to the structure\n"
            "  copy              — scaffold the CTD structure for a specific drug program in GCS\n"
            "  write             — generate / fill CTD section content (full pipeline)\n"
            "  status            — ask what is currently loaded or running\n"
            "  generate_template — generate ICH-grounded section templates only (no full write)\n"
            "  rewrite_section   — regenerate one or more specific CTD sections\n"
            "  analyze_data      — analyse clinical trial data / CSV for a program\n"
            "  help              — anything else / unclear"
        )
    )
    force_reextract: bool = Field(
        default=False,
        description="True when the user explicitly wants to rebuild/refresh (re-extract, refresh, rebuild).",
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
    module_filter: list[str] = Field(
        default_factory=list,
        description=(
            "For generate_template only: restrict to specific module keys, "
            "e.g. ['module5']. Empty list means all modules."
        ),
    )


class CoordinatorDecision(BaseModel):
    """
    Final coordinator output consumed by app.py.

    outcome == 'clarify'
        reply is sent directly to the user; no action handler is called.
        The clarify message may:
          - ask for missing program fields (ta / disease / drug)
          - explain which prerequisite step is missing and how to do it
          - ask for disapproval feedback when none was captured

    outcome == 'proceed'
        All prerequisites are met; app.py dispatches to the matching
        action handler (_do_extract, _do_approve, etc.).
    """
    outcome: Literal["clarify", "proceed"] = Field(
        description="'clarify' — ask user something; 'proceed' — run the action."
    )
    # ── resolved intent (always set) ─────────────────────────────────────────
    intent: Literal["extract", "approve", "disapprove", "copy", "write", "status",
                    "generate_template", "rewrite_section", "analyze_data", "help"] = Field(
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
    module_filter: list[str] = Field(default_factory=list)
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
  write             — run the full CTD content generation pipeline for a program
  status            — ask what is currently loaded or running in the background
  generate_template — generate ICH-grounded section templates only (preview before writing)
                      e.g. "show templates for module 5", "preview the CTD template"
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
  section_keys        — for rewrite_section only: list of CTD section keys, e.g. ['2.5_clinical_overview']
  module_filter       — for generate_template only: list of module keys, e.g. ['module5']

IMPORTANT: messages often use a slash-separated triplet:
  "<therapeutic_area> / <disease_type> / <drug_name>"
  e.g. "oncology / lung cancer / carboplatin" → ta=oncology, disease=lung cancer, drug=carboplatin
  Always extract all three fields from this pattern.
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
  RULE: if paths_loaded > 0 → ALWAYS outcome=proceed. NEVER block on canonical_exists or approved.
  If paths_loaded=0 → clarify: ask the user to extract the ICH structure first.
  IMPORTANT: approval is the action that CREATES the canonical default.
             It CANNOT and MUST NOT require canonical_exists=true as a prerequisite.
             canonical_exists=false is the normal state BEFORE approval — it is not an error.

disapprove:
  PREREQUISITE: paths_loaded > 0.
  If paths_loaded=0 → clarify: ask the user to extract first.

copy (scaffold a program directory):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed. No other prerequisites.
  NOTE: scaffolding always reads from the shared canonical default CTD structure in GCS.
        Approval is NOT required. canonical_exists and session paths are irrelevant.
        The action handler will check GCS at runtime and report any error.
  If any program field missing → clarify: ask only for the missing fields, warmly.

write (generate content):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed. No other prerequisites.
  NOTE: approval is NOT required. Scaffolding will be done automatically if needed.
        The action handler will check GCS at runtime and report any error.
  If program fields missing → clarify: ask for the missing fields, warmly.

generate_template (template preview only):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed.
  NOTE: generates ICH-grounded section templates and stores them in GCS.
        Does NOT trigger the content writing pipeline.
  If program fields missing → clarify.

rewrite_section (regenerate specific sections):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed.
  NOTE: section_keys is optional — empty means rewrite all sections.
  If program fields missing → clarify.

analyze_data (clinical data analysis):
  PREREQUISITE: therapeutic_area, disease_type, drug_name must all be known.
  RULE: if all 3 fields are present → ALWAYS outcome=proceed.
  NOTE: the analyst will read clinical CSV from GCS for the given program.
        It does NOT require a prior write or template generation step.
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
    # The LLM sometimes mis-routes these despite correct context; this override is deterministic.
    _m = re.search(r"paths_loaded=(\d+)", state.get("workflow_context", ""))
    _canonical_true = "canonical_exists=True" in state.get("workflow_context", "")
    if (decision.outcome == "clarify"
            and ir.intent in ("approve", "disapprove")
            and _m and int(_m.group(1)) > 0):
        decision = CoordinatorDecision(outcome="proceed", intent=ir.intent)

    # Hard guardrail: copy/write/template/rewrite/analyze with all 3 fields must always
    # proceed — the action handler does the real GCS check at runtime.
    _has_all_fields = bool(ir.therapeutic_area and ir.disease_type and ir.drug_name)
    if (decision.outcome == "clarify"
            and ir.intent in ("copy", "write", "generate_template", "rewrite_section", "analyze_data")
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
    if not decision.module_filter and ir.module_filter:
        decision.module_filter = ir.module_filter
    return {"decision": decision}


# ── Build and compile the graph ───────────────────────────────────────────────

_graph = StateGraph(_CoordState)
_graph.add_node("understand", _understand_node)
_graph.add_node("decide",     _decide_node)
_graph.set_entry_point("understand")
_graph.add_edge("understand", "decide")
_graph.add_edge("decide", END)

coordinator_app = _graph.compile()


# ── Public helper ─────────────────────────────────────────────────────────────

def run_coordinator(message: str, session_state: dict) -> CoordinatorDecision:
    """
    Run the two-node coordinator graph.

    Parameters
    ----------
    message       : raw user message
    session_state : the Gradio gr.State dict (folder_paths, approved,
                    bucket, session_id, content_program,
                    program_scaffold_exists, ...)

    Returns a CoordinatorDecision.  Falls back to a 'proceed/help' decision
    on any LLM error so the UI never hard-fails.
    """
    st = session_state or {}
    paths    = st.get("folder_paths", [])
    approved = st.get("approved", False)
    prog     = st.get("content_program") or {}

    # Build a rich but compact context string for the LLM
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

    try:
        out = coordinator_app.invoke({
            "message":          message,
            "workflow_context": workflow_context,
            "intent_result":    None,
            "decision":         None,
        })
        return out["decision"]
    except Exception as exc:
        print(f"[coordinator] LLM failed, defaulting to help: {exc}")
        return CoordinatorDecision(outcome="proceed", intent="help")


# ── Backwards-compat shim ─────────────────────────────────────────────────────
# app.py's disapproval-feedback fast-path still calls parse_intent to check
# for explicit extract keywords — keep it working with a thin wrapper.

def parse_intent(message: str, state: dict) -> "IntentResult":
    """Thin shim — used only by the awaiting_feedback fast-path in app.py."""
    try:
        llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        return llm.with_structured_output(IntentResult).invoke([
            SystemMessage(content=_UNDERSTAND_SYSTEM),
            HumanMessage(content=(
                f"Session context: paths_loaded={len((state or {}).get('folder_paths', []))}, "
                f"approved={(state or {}).get('approved', False)}\n\n"
                f"User message: {message}"
            )),
        ])
    except Exception as exc:
        print(f"[coordinator] parse_intent shim failed: {exc}")
        return IntentResult(intent="help")
