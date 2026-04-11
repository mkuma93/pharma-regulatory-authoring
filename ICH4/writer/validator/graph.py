"""LangGraph validator graph for cross-module CTD consistency checking."""
from __future__ import annotations

from functools import partial
from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END

from .nodes import (
    extract_key_values,
    check_consistency,
    auto_fill_from_cross_sections,
    check_unfilled_placeholders,
    check_not_filled_markers,
    check_ich_coverage,
    llm_deep_check,
    finalise,
)
from .state import ValidatorState
from writer.models import ValidationResult


def build_graph(llm: ChatOpenAI) -> Any:
    """Return a compiled LangGraph for CTD cross-module validation.

    Pipeline:
      extract_key_values
        → check_consistency              (drug name, demographics, efficacy, safety)
        → auto_fill_from_cross_sections  (fill DATA PENDING from consensus values)
        → check_unfilled_placeholders    (flag remaining DATA PENDING)
        → check_not_filled_markers       (flag NOT FILLED template markers)
        → check_ich_coverage             (ICH M4 mandatory element presence per section)
        → llm_deep_check                 (benefit-risk alignment, claim support,
                                          regulatory language, narrative coherence)
        → finalise
    """
    graph = StateGraph(ValidatorState)

    graph.add_node("extract_key_values",          extract_key_values)
    graph.add_node("check_consistency",            check_consistency)
    graph.add_node("auto_fill",                    auto_fill_from_cross_sections)
    graph.add_node("check_unfilled",               check_unfilled_placeholders)
    graph.add_node("check_not_filled",             check_not_filled_markers)
    graph.add_node("check_ich_coverage",           partial(check_ich_coverage, llm=llm))
    graph.add_node("llm_deep_check",               partial(llm_deep_check, llm=llm))
    graph.add_node("finalise",                     finalise)

    graph.add_edge(START,                  "extract_key_values")
    graph.add_edge("extract_key_values",   "check_consistency")
    graph.add_edge("check_consistency",    "auto_fill")
    graph.add_edge("auto_fill",            "check_unfilled")
    graph.add_edge("check_unfilled",       "check_not_filled")
    graph.add_edge("check_not_filled",     "check_ich_coverage")
    graph.add_edge("check_ich_coverage",   "llm_deep_check")
    graph.add_edge("llm_deep_check",       "finalise")
    graph.add_edge("finalise",             END)

    return graph.compile()


def run_validator(documents: list, llm: ChatOpenAI) -> ValidationResult:
    """Execute the validator graph and return a `ValidationResult`.

    Args:
        documents: list[SectionDocument] — written sections to validate.
        llm:       ChatOpenAI instance shared with the writer.
    """
    compiled = build_graph(llm)

    initial_state = ValidatorState(
        documents=documents,
        issues=[],
        messages=[],
        extracted_values={},
        canonical_values={},
        drug_name_found="",
        passed=True,
        summary="",
    )

    final_state: ValidatorState = compiled.invoke(initial_state)

    return ValidationResult(
        passed=final_state.passed,
        issues=final_state.issues,
        summary=final_state.summary,
    )
