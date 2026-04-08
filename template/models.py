"""
template/models.py

Pydantic models for the per-program CTD content template system.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProgramInfo(BaseModel):
    """Identifies a drug program that templates are generated for."""

    therapeutic_area: str
    disease_type: str
    drug_name: str

    @property
    def gcs_prefix(self) -> str:
        """GCS prefix where all templates for this program are stored.

        e.g. therapeutic-area/oncology/lung_cancer/carboplatin/templates
        """
        ta   = self.therapeutic_area.strip().lower().replace(" ", "_")
        dis  = self.disease_type.strip().lower().replace(" ", "_")
        drug = self.drug_name.strip().lower().replace(" ", "_")
        return f"therapeutic-area/{ta}/{dis}/{drug}/templates"


class SectionTemplate(BaseModel):
    """Markdown content template for one CTD section."""

    module_key: str
    module_label: str
    section_key: str
    section_label: str
    content: str = Field(..., description="Full markdown template with {{placeholder}} syntax")

    @property
    def gcs_path_suffix(self) -> str:
        """Relative path within the program's templates prefix.

        e.g. module2/2.5_clinical_overview.md
        """
        return f"{self.module_key}/{self.section_key}.md"


class TemplateManifest(BaseModel):
    """Metadata written alongside the generated templates."""

    program: ProgramInfo
    generated_date: str
    total_templates: int
    template_paths: list[str] = Field(default_factory=list)
