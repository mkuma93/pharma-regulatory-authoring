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


class OrchestratorResponse(BaseModel):
    """Response from POST /generate."""
    templates: list[SectionTemplate]
    modules_generated: list[str]
    ich_index_used: bool
    clinical_data_used: bool
