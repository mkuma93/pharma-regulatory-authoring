"""
ctd_structure/structure.py  (ICH4/template service — Pydantic stub)

Minimal data models that satisfy the import in template/generator.py:
    from ctd_structure.structure import CTDStructureOutput
No LangGraph, no smtplib, no requests — just Pydantic.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CTDSubsection(BaseModel):
    key: str
    label: str
    sub_subsections: list["CTDSubsection"] = Field(default_factory=list)


CTDSubsection.model_rebuild()


class CTDSection(BaseModel):
    key: str
    label: str
    subsections: list[CTDSubsection] = Field(default_factory=list)


CTDSection.model_rebuild()


class CTDModule(BaseModel):
    key: str
    label: str
    sections: list[CTDSection] = Field(default_factory=list)


CTDModule.model_rebuild()


class EvaluationIssue(BaseModel):
    path: str
    level: str
    reason: str


class EvaluationResult(BaseModel):
    passed: bool
    issues: list[EvaluationIssue] = Field(default_factory=list)
    summary: str = ""


EvaluationResult.model_rebuild()


class CTDStructureOutput(BaseModel):
    """Minimal stub — exposes only .modules for duck-typed iteration."""
    modules: list[CTDModule] = Field(default_factory=list)
