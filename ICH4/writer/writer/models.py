"""Pydantic models for the writer service."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProgramInfo(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str


class SectionDocument(BaseModel):
    """A fully written CTD section — placeholders replaced with real content."""
    module_key: str
    module_label: str
    section_key: str
    section_label: str
    content: str = Field(..., description="Final regulatory prose, no remaining {{placeholders}}")
    gcs_path: str = Field(default="", description="GCS path where this document was saved")
    prompt_messages: list[dict] = Field(
        default_factory=list,
        description="Serialised LLM messages (system + user) that produced this document.",
        exclude=True,  # not included in API response
    )


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    section_key: str
    message: str


class ValidationResult(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: str = ""


class WriterRequest(BaseModel):
    program: ProgramInfo
    bucket_name: str = Field(..., description="GCS bucket with templates + clinical data")
    sections: list[str] = Field(
        default_factory=list,
        description="Section keys to write. Empty = all templates found.",
    )
    run_validator: bool = Field(
        default=True,
        description="Run LangGraph cross-module consistency validator after writing.",
    )
    resolved_values: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Pre-computed placeholder values from clinical-analyst /resolve. "
            "Keys present here will not be re-dispatched to the hybrid analyst."
        ),
    )
    run_id: str = Field(
        default="",
        description="Content-generation run identifier — stored in version manifest.",
    )
    author: str = Field(
        default="",
        description="User who triggered the write, extracted from IAP header.",
    )


class WriterResponse(BaseModel):
    documents: list[SectionDocument]
    validation: ValidationResult
    sections_written: int
    sections_failed: list[str] = Field(default_factory=list)
