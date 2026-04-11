"""
models.py — shared Pydantic schemas for the orchestrator.

These are intentionally self-contained (no imports from template/ or clinical/)
so the orchestrator only depends on httpx and pydantic.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProgramInfo(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str


class SectionTemplate(BaseModel):
    module_key: str
    module_label: str
    section_key: str
    section_label: str
    content: str


class OrchestratorRequest(BaseModel):
    """Body for POST /generate."""
    program: ProgramInfo
    include_clinical_data: bool = Field(
        default=True,
        description="Attempt to load clinical manifest from the clinical service.",
    )
    include_ich_context: bool = Field(
        default=True,
        description="Query the index service for ICH guideline context per module.",
    )
    module_filter: list[str] = Field(
        default_factory=list,
        description=(
            "Restrict generation to these module keys (e.g. ['module5']). "
            "Empty list = all modules."
        ),
    )
    evidence_namespaces: list[str] = Field(
        default_factory=list,
        description=(
            "Per-program index namespaces to query for prior-pass evidence. "
            "Format: '{base_ns}_{pass_id}', e.g. "
            "'program_neurology_bells_palsy_prednisolone_module5'. "
            "Evidence is injected into the template prompt to ground Module 2 "
            "summaries in actual CSR content (ICH M4E(R2) evidence chain)."
        ),
    )
    section_key_prefixes: list[str] = Field(
        default_factory=list,
        description=(
            "Only include sections whose key starts with one of these prefixes. "
            "Empty = all sections in the active modules. Used to split Module 2 "
            "into a 2.7 pass (Clinical Summary) and a 2.5/2.4/2.3 pass (Overviews)."
        ),
    )


class OrchestratorResponse(BaseModel):
    """Response from POST /generate."""
    templates: list[SectionTemplate]
    modules_generated: list[str]
    ich_index_used: bool
    clinical_data_used: bool
