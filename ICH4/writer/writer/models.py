"""Pydantic models for the writer service."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_PROGRAM_FIELD_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,127}$")


def _validate_program_field(v: str, field_name: str) -> str:
    """Reject values that could be used for path traversal."""
    normalized = v.strip().lower().replace(" ", "_")
    if not _PROGRAM_FIELD_RE.match(normalized):
        raise ValueError(
            f"{field_name} must contain only letters, digits, hyphens and "
            f"underscores (got {v!r})"
        )
    if ".." in normalized or "/" in normalized or "\\" in normalized:
        raise ValueError(f"{field_name} must not contain path separators")
    return normalized


class ProgramInfo(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str

    @field_validator("therapeutic_area", mode="before")
    @classmethod
    def _check_ta(cls, v: str) -> str:
        return _validate_program_field(str(v), "therapeutic_area")

    @field_validator("disease_type", mode="before")
    @classmethod
    def _check_dis(cls, v: str) -> str:
        return _validate_program_field(str(v), "disease_type")

    @field_validator("drug_name", mode="before")
    @classmethod
    def _check_drug(cls, v: str) -> str:
        return _validate_program_field(str(v), "drug_name")


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
