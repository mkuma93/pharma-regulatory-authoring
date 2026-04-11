"""POST /generate — generate CTD section templates for a program.

The orchestrator pre-fetches:
  • ICH guideline context   (ich_context: keyed by module_key, e.g. "module2" → text)
  • Clinical data manifest  (clinical_manifest: ClinicalDataManifest JSON)

and passes them here. This service is responsible only for the LLM template
generation step; it does not call the index or GCS directly.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from clinical.models import ClinicalDataManifest
from clinical.storage import load_manifest
from config.settings import settings
from ctd_structure.structure import CTDModule, CTDSection, CTDSubsection
from template.generator import generate_program_templates
from template.models import ProgramInfo, SectionTemplate

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Demo CTD structure: module2 + module5 ─────────────────────────────────────

def _build_demo_modules() -> list[CTDModule]:
    return [
        CTDModule(
            key="module2",
            label="CTD Summaries",
            sections=[
                CTDSection(key="2.1_ctd_table_of_contents",
                           label="CTD Table of Contents"),
                CTDSection(key="2.2_introduction",
                           label="Introduction to the Summary Documents"),
                CTDSection(key="2.3_quality_overall_summary",
                           label="Quality Overall Summary"),
                CTDSection(key="2.4_nonclinical_overview",
                           label="Nonclinical Overview"),
                CTDSection(
                    key="2.5_clinical_overview",
                    label="Clinical Overview",
                    subsections=[
                        CTDSubsection(key="2.5.1_product_development_rationale",
                                      label="Product Development Rationale"),
                        CTDSubsection(key="2.5.2_overview_of_biopharmaceutics",
                                      label="Overview of Biopharmaceutics"),
                        CTDSubsection(key="2.5.3_overview_of_clinical_pharmacology",
                                      label="Overview of Clinical Pharmacology"),
                        CTDSubsection(key="2.5.4_overview_of_efficacy",
                                      label="Overview of Efficacy"),
                        CTDSubsection(key="2.5.5_overview_of_safety",
                                      label="Overview of Safety"),
                        CTDSubsection(key="2.5.6_benefit_risk_conclusions",
                                      label="Benefit–Risk Conclusions"),
                        CTDSubsection(key="2.5.7_literature_references",
                                      label="Literature References"),
                    ],
                ),
                CTDSection(
                    key="2.6_nonclinical_written_and_tabulated_summaries",
                    label="Nonclinical Written and Tabulated Summaries",
                    subsections=[
                        CTDSubsection(key="2.6.1_pharmacology_written_summary",
                                      label="Pharmacology Written Summary"),
                        CTDSubsection(key="2.6.2_pharmacology_tabulated_summary",
                                      label="Pharmacology Tabulated Summary"),
                        CTDSubsection(key="2.6.3_pharmacokinetics_written_summary",
                                      label="Pharmacokinetics Written Summary"),
                        CTDSubsection(key="2.6.4_pharmacokinetics_tabulated_summary",
                                      label="Pharmacokinetics Tabulated Summary"),
                        CTDSubsection(key="2.6.5_toxicology_written_summary",
                                      label="Toxicology Written Summary"),
                        CTDSubsection(key="2.6.6_toxicology_tabulated_summary",
                                      label="Toxicology Tabulated Summary"),
                    ],
                ),
                CTDSection(
                    key="2.7_clinical_summary",
                    label="Clinical Summary",
                    subsections=[
                        CTDSubsection(
                            key="2.7.1_summary_of_biopharmaceutic_studies_and_analytical_methods",
                            label="Summary of Biopharmaceutic Studies and Associated Analytical Methods",
                        ),
                        CTDSubsection(key="2.7.2_summary_of_clinical_pharmacology_studies",
                                      label="Summary of Clinical Pharmacology Studies"),
                        CTDSubsection(key="2.7.3_summary_of_clinical_efficacy",
                                      label="Summary of Clinical Efficacy"),
                        CTDSubsection(key="2.7.4_summary_of_clinical_safety",
                                      label="Summary of Clinical Safety"),
                        CTDSubsection(key="2.7.5_literature_references",
                                      label="Literature References"),
                        CTDSubsection(key="2.7.6_synopsis_of_individual_studies",
                                      label="Synopsis of Individual Studies"),
                    ],
                ),
            ],
        ),
        CTDModule(
            key="module5",
            label="Clinical Study Reports",
            sections=[
                CTDSection(key="5.1_table_of_contents", label="Table of Contents"),
                CTDSection(key="5.2_tabular_listing_of_all_clinical_studies",
                           label="Tabular Listing of All Clinical Studies"),
                CTDSection(
                    key="5.3_clinical_study_reports",
                    label="Clinical Study Reports",
                    subsections=[
                        CTDSubsection(key="5.3.1_reports_of_biopharmaceutic_studies",
                                      label="Reports of Biopharmaceutic Studies"),
                        CTDSubsection(
                            key="5.3.2_reports_of_studies_pertinent_to_pk_using_human_biomaterials",
                            label="Reports of Studies Pertinent to Pharmacokinetics Using Human Biomaterials",
                        ),
                        CTDSubsection(key="5.3.3_reports_of_human_pharmacokinetic_studies",
                                      label="Reports of Human Pharmacokinetic (PK) Studies"),
                        CTDSubsection(key="5.3.4_reports_of_human_pharmacodynamic_studies",
                                      label="Reports of Human Pharmacodynamic (PD) Studies"),
                        CTDSubsection(key="5.3.5_reports_of_efficacy_and_safety_studies",
                                      label="Reports of Efficacy and Safety Studies"),
                        CTDSubsection(key="5.3.6_reports_of_post_marketing_experience",
                                      label="Reports of Post-Marketing Experience"),
                        CTDSubsection(key="5.3.7_case_report_forms_and_individual_patient_listings",
                                      label="Case Report Forms and Individual Patient Listings"),
                    ],
                ),
                CTDSection(key="5.4_literature_references", label="Literature References"),
            ],
        ),
    ]


class _CTDView:
    """Duck-typed stand-in for CTDStructureOutput — exposes only .modules."""
    def __init__(self, modules: list[CTDModule]) -> None:
        self.modules = modules


# ── Request / Response ────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    """Body for POST /generate."""
    program: ProgramInfo
    ich_context: dict[str, str] | None = None
    """ICH guideline text keyed by module_key (e.g. "module2" → retrieved ICH text).
    Pre-fetched by the orchestrator from the index service. None → LLM uses
    training memory and marks all requirements as [NOT RETRIEVED]."""
    include_clinical_data: bool = True
    """When True the template service loads the clinical manifest from GCS
    (using the bucket configured in settings). Set to False to skip."""
    section_key_prefixes: list[str] = Field(
        default_factory=list,
        description=(
            "Only generate sections whose key starts with one of these prefixes. "
            "Empty = all sections. Allows caller to run module2 in two ordered "
            "passes: first '2.7' (Clinical Summary), then '2.5'/'2.4'/'2.3' (Overviews)."
        ),
    )
    prior_evidence: dict[str, str] | None = Field(
        default=None,
        description=(
            "RAG evidence from previously generated CTD sections, keyed by program "
            "namespace (e.g. 'program_neurology_..._module5' → answer text). "
            "Injected into the LLM prompt so Module 2 summaries can cite actual "
            "CSR findings rather than hallucinating statistics."
        ),
    )


class GenerateResponse(BaseModel):
    """Successful response from POST /generate."""
    templates: list[SectionTemplate]
    modules_generated: list[str]
    ich_index_used: bool
    clinical_data_used: bool


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/generate", response_model=GenerateResponse)
def generate(body: GenerateRequest) -> GenerateResponse:
    """Generate ICH CTD section templates for module2 and module5."""
    # Fetch clinical manifest from GCS if requested
    clinical_manifest: ClinicalDataManifest | None = None
    if body.include_clinical_data and settings.gcs_bucket_name:
        clinical_manifest = load_manifest(settings.gcs_bucket_name, body.program)

    demo_modules = _build_demo_modules()

    # Apply section_key_prefixes: filter sections within each module,
    # then drop modules with no remaining sections.
    if body.section_key_prefixes:
        prefixes = body.section_key_prefixes
        for mod in demo_modules:
            mod.sections = [
                s for s in mod.sections
                if any(s.key.startswith(p) for p in prefixes)
            ]
        demo_modules = [m for m in demo_modules if m.sections]

    try:
        templates = generate_program_templates(
            program=body.program,
            ctd_output=_CTDView(demo_modules),  # type: ignore[arg-type]
            clinical_manifest=clinical_manifest,
            ich_context=body.ich_context,
            prior_evidence=body.prior_evidence,
            api_key=settings.openai_api_key,
            model=settings.llm_model,
        )
    except Exception as exc:
        logger.exception("[template-service] Generation failed.")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return GenerateResponse(
        templates=templates,
        modules_generated=[m.key for m in demo_modules],
        ich_index_used=body.ich_context is not None,
        clinical_data_used=clinical_manifest is not None,
    )
