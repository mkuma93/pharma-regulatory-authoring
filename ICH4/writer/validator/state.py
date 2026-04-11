"""LangGraph state for the cross-module consistency validator."""
from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from writer.models import SectionDocument, ValidationIssue


class ValidatorState(BaseModel):
    """State passed between validator nodes."""

    # Input + auto-fill output — list of written documents (may be mutated by auto_fill node)
    documents: list[SectionDocument] = Field(default_factory=list)

    # Issues accumulate across ALL check nodes via operator.add reducer
    issues: Annotated[list[ValidationIssue], operator.add] = Field(default_factory=list)

    # LangGraph messages for the LLM deep-check node
    messages: Annotated[list, add_messages] = Field(default_factory=list)

    # Per-section extracted values: {section_key: {field_name: value}}
    extracted_values: dict[str, dict[str, str]] = Field(default_factory=dict)

    # Consensus value per field (most common across sections)
    canonical_values: dict[str, str] = Field(default_factory=dict)

    # Pre-computed ground-truth values from clinical-analyst /resolve
    # {placeholder_key: computed_string}  e.g. {"recovery_rate": "71.2% (127/178)"}
    resolved_values: dict[str, str] = Field(default_factory=dict)

    # Legacy compat
    drug_name_found: str = ""

    # Final output
    passed: bool = True
    summary: str = ""
