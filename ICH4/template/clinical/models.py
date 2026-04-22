"""
clinical/models.py  (template service copy — models only, no GCS storage)

Used by template/generator.py to inject clinical context into prompts.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from template.models import ProgramInfo


class ColumnMapping(BaseModel):
    column_name: str = Field(..., description="Exact CSV column header name")
    role: str = Field(..., description="Regulatory role of this column in a CTD submission")
    ctd_section_keys: list[str] = Field(
        ...,
        description="ICH M4 CTD section keys where this column's data belongs",
    )
    placeholder_key: str = Field(
        ...,
        description="Snake-case identifier used as {{placeholder}} in CTD templates",
    )
    positive_value: str | None = Field(
        default=None,
        description=(
            "For categorical/binary columns: the value that counts as a positive outcome "
            "(e.g. 'Yes', 'Recovered', '1'). None for numeric columns."
        ),
    )


class ClinicalDataSource(BaseModel):
    filename: str = Field(..., description="Original CSV filename")
    gcs_path: str = Field(..., description="Full GCS path where the CSV is stored")
    study_type: Literal["RCT", "registry", "observational", "PK", "safety", "other"]
    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    ctd_section_keys: list[str] = Field(default_factory=list)


class ClinicalDataManifest(BaseModel):
    program: ProgramInfo
    sources: list[ClinicalDataSource] = Field(default_factory=list)
    last_updated: str = Field(default="")
