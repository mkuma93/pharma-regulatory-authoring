"""
clinical/models.py

Pydantic models for clinical data source registration and CTD placeholder mapping.
Each CSV file represents one clinical study (one ClinicalDataSource).
All sources for a program are collected in ClinicalDataManifest.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from template.models import ProgramInfo


class ColumnMapping(BaseModel):
    """Maps one CSV column to its regulatory role and CTD section placeholders."""

    column_name: str = Field(..., description="Exact CSV column header name")
    role: str = Field(..., description="Regulatory role of this column in a CTD submission")
    ctd_section_keys: list[str] = Field(
        ...,
        description=(
            "ICH M4 CTD section keys where this column's data belongs, "
            "e.g. ['5.3.5.1', '2.7.3', '2.5.4']"
        ),
    )
    placeholder_key: str = Field(
        ...,
        description=(
            "Snake-case identifier used as {{placeholder}} in CTD templates, "
            "e.g. 'recovery_rate_3mo'"
        ),
    )
    positive_value: str | None = Field(
        default=None,
        description=(
            "For categorical/binary columns: the value that counts as a positive outcome "
            "(e.g. 'Yes', 'Recovered', '1'). None for numeric columns."
        ),
    )


class ClinicalDataSource(BaseModel):
    """One registered clinical dataset (one CSV file = one study)."""

    filename: str = Field(..., description="Original CSV filename")
    gcs_path: str = Field(..., description="Full GCS path where the CSV is stored")
    study_type: Literal["RCT", "registry", "observational", "PK", "safety", "other"] = Field(
        ..., description="Type of clinical study"
    )
    column_mappings: list[ColumnMapping] = Field(
        default_factory=list,
        description="Mapping for each relevant column",
    )
    ctd_section_keys: list[str] = Field(
        default_factory=list,
        description="Sorted union of all ctd_section_keys across column_mappings",
    )


class ClinicalDataManifest(BaseModel):
    """Registry of all clinical datasets registered for a drug program."""

    program: ProgramInfo
    sources: list[ClinicalDataSource] = Field(default_factory=list)
    last_updated: str = Field(default="")
