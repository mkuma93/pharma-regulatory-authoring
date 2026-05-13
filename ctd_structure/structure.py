"""
ctd_structure/structure.py

Hybrid CTD structure builder:
  1. Canonical ICH M4(R4) folder keys are hardcoded — deterministic, no hallucination.
  2. Human-readable labels are optionally enriched by querying the ICH index.
  3. A LangGraph pipeline orchestrates: load → enrich → evaluate → notify.

Why hardcode keys?
  The ICH M4 folder hierarchy (module numbers, section codes like 3.2.S, 2.5.1…)
  is a published regulatory standard.  LLM responses produce inconsistent keys
  across calls (e.g. "3.2_s_drug_substance" vs "3.2.s_drug_substance" vs
  "3.2.S_Drug_Substance"), leading to duplicate GCS paths.  Keys are frozen here;
  only labels are enriched by the LLM.

Future-proofing:
  When ICH M4 is revised, update _ICH_M4_CANONICAL below and redeploy.  The LLM
  enrichment step will automatically reflect any label changes from the index.

Graph
─────
  START ──► load_canonical ──► enrich_labels ──► evaluate_completeness ──► notify_human ──► END

Public API (signatures unchanged):
  extract_from_ich_index(ich_index_url, reviewer_email) → (CTDStructureOutput, EvaluationResult)
  refine_from_feedback(ich_index_url, feedback, current_output) → (CTDStructureOutput, EvaluationResult)
"""
from __future__ import annotations  # allows | union syntax in type hints on Python 3.9

import json       # parsing LLM JSON responses from the ICH index
import os         # reading SMTP_HOST, SMTP_PORT, CTD_REVIEWER_EMAIL env vars
import re         # stripping markdown fences from LLM responses in _extract_json_object
import smtplib    # sending plain-SMTP approval emails in _send_approval_email
import subprocess # running 'gcloud auth print-identity-token' for local dev auth
from email.mime.multipart import MIMEMultipart  # constructing multipart MIME email
from email.mime.text import MIMEText            # attaching plain-text body to MIME email

import requests                          # HTTP POST to ICH index /index/query endpoint
from langgraph.graph import END, StateGraph  # END sentinel and graph builder for the pipeline
from pydantic import BaseModel, Field    # domain models with schema validation
from typing_extensions import TypedDict  # LangGraph state dict — must be TypedDict, not Pydantic


# ── Pydantic output models ────────────────────────────────────────────────────

class CTDSubsection(BaseModel):
    """Represents a CTD subsection or sub-subsection folder (levels 3 and 4).

    This model is self-referential: sub_subsections holds nested CTDSubsection
    objects, which allows the same class to represent both subsections (depth 3)
    and sub-subsections (depth 4) without a separate model for each level.
    """
    key: str = Field(description="Folder-safe key, e.g. '2.5.1_product_development_rationale'")
    label: str = Field(description="Human-readable title")
    sub_subsections: list["CTDSubsection"] = Field(default_factory=list)


# Required: CTDSubsection contains list["CTDSubsection"] — a forward reference
# to itself.  Pydantic v2 cannot resolve this at class-definition time, so
# model_rebuild() is called immediately after the class body to finalise
# the schema and allow nested instantiation.
CTDSubsection.model_rebuild()


class CTDSection(BaseModel):
    """Represents a CTD section folder (level 2, e.g. 2.5_clinical_overview).

    Holds a flat list of CTDSubsection objects; nesting beyond this point is
    handled recursively by CTDSubsection.sub_subsections.
    """
    key: str = Field(description="Folder-safe key, e.g. '2.5_clinical_overview'")
    label: str = Field(description="Human-readable title")
    subsections: list[CTDSubsection] = Field(default_factory=list)


# Defensive: CTDSection is defined after CTDSubsection.model_rebuild(), so
# all types are already resolved at class-definition time and this call is
# technically redundant.  It is kept to make the rebuild chain explicit and
# to guard against future reordering of the class definitions.
CTDSection.model_rebuild()


class CTDModule(BaseModel):
    """Represents one of the five top-level ICH M4(R4) CTD modules (level 1).

    key follows the pattern 'module1' … 'module5'.  Each module holds a list
    of CTDSection objects; the full 4-level hierarchy is reached by traversing
    CTDSection.subsections and then CTDSubsection.sub_subsections.
    """
    key: str = Field(description="Folder-safe key, e.g. 'module2'")
    label: str = Field(description="Module title")
    sections: list[CTDSection] = Field(default_factory=list)


# Defensive: same reasoning as CTDSection.model_rebuild() above — not required
# by Pydantic v2 at this point in the file, but kept for symmetry and safety.
CTDModule.model_rebuild()


class EvaluationIssue(BaseModel):
    path: str    # slash-separated folder path of the offending node, e.g. 'module3/3.2.S_drug_substance'
    level: str   # granularity of the issue: "module" | "section" | "subsection" | "sub_subsection"
    reason: str  # human-readable explanation of why this path failed validation


class EvaluationResult(BaseModel):
    passed: bool                                              # True only when issues list is empty
    issues: list[EvaluationIssue] = Field(default_factory=list)  # all structural problems found
    summary: str = ""                                         # one-paragraph human-readable verdict


EvaluationResult.model_rebuild()  # defensive rebuild — EvaluationIssue is already resolved but kept for symmetry


class CTDStructureOutput(BaseModel):
    """Full CTD structure assembled from all five ICH M4 modules."""

    modules: list[CTDModule]
    evaluation: EvaluationResult | None = None

    def to_folder_paths(self) -> list[str]:
        """Flatten all ICH M4 levels to relative paths for GCS / os.makedirs.

        Recursive — handles the static 4-level ICH template AND any additional
        program-specific study sub-folders injected by expand_program_studies
        at depth 5+.
        """
        paths: list[str] = []  # accumulates every folder path in depth-first order

        def _recurse(prefix: str, sub: "CTDSubsection") -> None:
            p = f"{prefix}{sub.key}/"  # append this subsection's key with trailing slash
            paths.append(p)            # record this level before descending into children
            for child in sub.sub_subsections:
                _recurse(p, child)     # recurse: handles depth 4 (sub-subsection) and beyond

        for module in self.modules:
            mp = f"ctd/{module.key}/"   # level 1: e.g. 'ctd/module2/'
            paths.append(mp)
            for section in module.sections:
                sp = f"{mp}{section.key}/"  # level 2: e.g. 'ctd/module2/2.5_clinical_overview/'
                paths.append(sp)
                for sub in section.subsections:
                    _recurse(sp, sub)   # level 3+: delegates to recursive helper
        return paths


CTDStructureOutput.model_rebuild()


# ── Canonical ICH M4(R4) structure ────────────────────────────────────────────
# Keys are authoritative folder names — never generated by an LLM.
# Labels are human-readable defaults; the enrich_labels node may improve them
# using the ICH index but will never change the keys.
#
# Structure: list of (module_key, module_label, [ (section_key, section_label, [ (sub_key, sub_label) ]) ])
#
# Last updated: ICH M4(R4) — update this dict when the standard is revised.

# ---------------------------------------------------------------------------
# ICH M4(R4) complete canonical folder hierarchy
#
# Structure per tuple:
#   (folder_key, human_label, [children_tuples])
#
# The hierarchy goes 4 levels deep where ICH defines sub-subsections:
#   module → section → subsection → sub-subsection
#
# Depth mapping in _build_canonical_modules:
#   level-0 = module       → CTDModule
#   level-1 = section      → CTDSection
#   level-2 = subsection   → CTDSubsection  (stored under CTDSection.subsections)
#   level-3 = sub-subsection → CTDSubsection (stored under CTDSubsection as a nested
#             list; to_folder_paths() flattens all four levels)
#
# Module 5 note:
#   ICH M4E defines fixed *study-type* sub-folders under 5.3.x (e.g. 5.3.1.1,
#   5.3.1.2 …).  Individual study PDFs go *inside* those folders; they are not
#   part of the template and are created per-program.  The static template
#   therefore includes every numbered sub-subfolder so the directory tree is
#   complete and consistent for all programs.
# ---------------------------------------------------------------------------

_ICH_M4_CANONICAL: list[tuple] = [

    # ── Module 1 — Regional Administrative Information ───────────────────────
    # ICH M4 does not prescribe deep sub-subsections for M1; structure is
    # region-specific.  Common sub-levels are included for completeness.
    ("module1", "Regional Administrative Information", [
        ("1.0_cover_letter",
         "Cover Letter", []),
        ("1.1_comprehensive_table_of_contents",
         "Comprehensive Table of Contents", []),
        ("1.2_application_form",
         "Application Form", []),
        ("1.3_product_information",
         "Product Information", [
             ("1.3.1_summary_of_product_characteristics",
              "Summary of Product Characteristics (SmPC)", []),
             ("1.3.2_labelling",
              "Labelling", []),
             ("1.3.3_package_leaflet",
              "Package Leaflet", []),
         ]),
        ("1.4_information_about_experts",
         "Information About Experts", [
             ("1.4.1_quality",
              "Quality Expert Report", []),
             ("1.4.2_nonclinical",
              "Nonclinical Expert Report", []),
             ("1.4.3_clinical",
              "Clinical Expert Report", []),
         ]),
        ("1.5_specific_requirements_for_application_types",
         "Specific Requirements for Different Types of Applications", [
             ("1.5.1_documentation_for_bibliographic_applications",
              "Documentation for Bibliographic Applications", []),
             ("1.5.2_documentation_for_generic_applications",
              "Documentation for Generic Applications", []),
             ("1.5.3_documentation_for_well_established_use",
              "Documentation for Well-Established Use", []),
             ("1.5.4_documentation_for_exceptional_circumstances",
              "Documentation for Exceptional Circumstances", []),
             ("1.5.5_documentation_for_informed_consent",
              "Documentation for Applications Based on Informed Consent", []),
             ("1.5.6_documentation_for_hybrid_applications",
              "Documentation for Hybrid Applications", []),
             ("1.5.7_documentation_for_biosimilar_applications",
              "Documentation for Biosimilar Applications", []),
         ]),
        ("1.6_environmental_risk_assessment",
         "Environmental Risk Assessment", []),
        ("1.7_information_relating_to_orphan_market_exclusivity",
         "Information Relating to Orphan Market Exclusivity", []),
        ("1.8_information_relating_to_pharmacovigilance",
         "Information Relating to Pharmacovigilance", [
             ("1.8.1_risk_management_system",
              "Risk Management System", []),
             ("1.8.2_pharmacovigilance_system_master_file",
              "Pharmacovigilance System Master File", []),
         ]),
        ("1.9_information_relating_to_clinical_trials",
         "Information Relating to Clinical Trials", []),
        ("1.10_information_relating_to_paediatrics",
         "Information Relating to Paediatrics", []),
        ("1.11_information_relating_to_fees",
         "Information Relating to Fees", []),
        ("1.12_other_regional_information",
         "Other Regional Information", []),
        ("1.13_information_relating_to_good_manufacturing_practice",
         "Information Relating to Good Manufacturing Practice", []),
    ]),

    # ── Module 2 — CTD Summaries ─────────────────────────────────────────────
    ("module2", "CTD Summaries", [
        ("2.1_ctd_table_of_contents",
         "CTD Table of Contents", []),
        ("2.2_introduction",
         "Introduction to the Summary Documents", []),
        ("2.3_quality_overall_summary",
         "Quality Overall Summary", []),
        ("2.4_nonclinical_overview",
         "Nonclinical Overview", []),
        ("2.5_clinical_overview",
         "Clinical Overview", [
             ("2.5.1_product_development_rationale",
              "Product Development Rationale", []),
             ("2.5.2_overview_of_biopharmaceutics",
              "Overview of Biopharmaceutics", []),
             ("2.5.3_overview_of_clinical_pharmacology",
              "Overview of Clinical Pharmacology", []),
             ("2.5.4_overview_of_efficacy",
              "Overview of Efficacy", []),
             ("2.5.5_overview_of_safety",
              "Overview of Safety", []),
             ("2.5.6_benefit_risk_conclusions",
              "Benefit–Risk Conclusions", []),
             ("2.5.7_literature_references",
              "Literature References", []),
         ]),
        ("2.6_nonclinical_written_and_tabulated_summaries",
         "Nonclinical Written and Tabulated Summaries", [
             ("2.6.1_pharmacology_written_summary",
              "Pharmacology Written Summary", []),
             ("2.6.2_pharmacology_tabulated_summary",
              "Pharmacology Tabulated Summary", []),
             ("2.6.3_pharmacokinetics_written_summary",
              "Pharmacokinetics Written Summary", []),
             ("2.6.4_pharmacokinetics_tabulated_summary",
              "Pharmacokinetics Tabulated Summary", []),
             ("2.6.5_toxicology_written_summary",
              "Toxicology Written Summary", []),
             ("2.6.6_toxicology_tabulated_summary",
              "Toxicology Tabulated Summary", []),
         ]),
        ("2.7_clinical_summary",
         "Clinical Summary", [
             ("2.7.1_summary_of_biopharmaceutic_studies_and_analytical_methods",
              "Summary of Biopharmaceutic Studies and Associated Analytical Methods",
              []),
             ("2.7.2_summary_of_clinical_pharmacology_studies",
              "Summary of Clinical Pharmacology Studies", []),
             ("2.7.3_summary_of_clinical_efficacy",
              "Summary of Clinical Efficacy", []),
             ("2.7.4_summary_of_clinical_safety",
              "Summary of Clinical Safety", []),
             ("2.7.5_literature_references",
              "Literature References", []),
             ("2.7.6_synopsis_of_individual_studies",
              "Synopsis of Individual Studies", []),
         ]),
    ]),

    # ── Module 3 — Quality ───────────────────────────────────────────────────
    # Sub-subsections follow ICH M4Q(R1) Table of Contents exactly.
    ("module3", "Quality", [
        ("3.1_table_of_contents",
         "Table of Contents", []),

        # 3.2.S — Drug Substance
        ("3.2.S_drug_substance",
         "Drug Substance", [
             ("3.2.S.1_general_information",
              "General Information", [
                  ("3.2.S.1.1_nomenclature",
                   "Nomenclature", []),
                  ("3.2.S.1.2_structure",
                   "Structure", []),
                  ("3.2.S.1.3_general_properties",
                   "General Properties", []),
              ]),
             ("3.2.S.2_manufacture",
              "Manufacture", [
                  ("3.2.S.2.1_manufacturer",
                   "Manufacturer(s)", []),
                  ("3.2.S.2.2_description_of_manufacturing_process_and_process_controls",
                   "Description of Manufacturing Process and Process Controls", []),
                  ("3.2.S.2.3_control_of_materials",
                   "Control of Materials", []),
                  ("3.2.S.2.4_controls_of_critical_steps_and_intermediates",
                   "Controls of Critical Steps and Intermediates", []),
                  ("3.2.S.2.5_process_validation_and_or_evaluation",
                   "Process Validation and/or Evaluation", []),
                  ("3.2.S.2.6_manufacturing_process_development",
                   "Manufacturing Process Development", []),
              ]),
             ("3.2.S.3_characterisation",
              "Characterisation", [
                  ("3.2.S.3.1_elucidation_of_structure_and_other_characteristics",
                   "Elucidation of Structure and Other Characteristics", []),
                  ("3.2.S.3.2_impurities",
                   "Impurities", []),
              ]),
             ("3.2.S.4_control_of_drug_substance",
              "Control of Drug Substance", [
                  ("3.2.S.4.1_specification",
                   "Specification", []),
                  ("3.2.S.4.2_analytical_procedures",
                   "Analytical Procedures", []),
                  ("3.2.S.4.3_validation_of_analytical_procedures",
                   "Validation of Analytical Procedures", []),
                  ("3.2.S.4.4_batch_analyses",
                   "Batch Analyses", []),
                  ("3.2.S.4.5_justification_of_specification",
                   "Justification of Specification", []),
              ]),
             ("3.2.S.5_reference_standards_or_materials",
              "Reference Standards or Materials", []),
             ("3.2.S.6_container_closure_system",
              "Container Closure System", []),
             ("3.2.S.7_stability",
              "Stability", [
                  ("3.2.S.7.1_stability_summary_and_conclusions",
                   "Stability Summary and Conclusions", []),
                  ("3.2.S.7.2_post_approval_stability_protocol_and_stability_commitment",
                   "Post-Approval Stability Protocol and Stability Commitment", []),
                  ("3.2.S.7.3_stability_data",
                   "Stability Data", []),
              ]),
         ]),

        # 3.2.P — Drug Product
        ("3.2.P_drug_product",
         "Drug Product", [
             ("3.2.P.1_description_and_composition_of_the_drug_product",
              "Description and Composition of the Drug Product", []),
             ("3.2.P.2_pharmaceutical_development",
              "Pharmaceutical Development", [
                  ("3.2.P.2.1_components_of_the_drug_product",
                   "Components of the Drug Product", []),
                  ("3.2.P.2.2_drug_product",
                   "Drug Product", []),
                  ("3.2.P.2.3_manufacturing_process_development",
                   "Manufacturing Process Development", []),
                  ("3.2.P.2.4_container_closure_system",
                   "Container Closure System", []),
                  ("3.2.P.2.5_microbiological_attributes",
                   "Microbiological Attributes", []),
                  ("3.2.P.2.6_compatibility",
                   "Compatibility", []),
              ]),
             ("3.2.P.3_manufacture",
              "Manufacture", [
                  ("3.2.P.3.1_manufacturer",
                   "Manufacturer(s)", []),
                  ("3.2.P.3.2_batch_formula",
                   "Batch Formula", []),
                  ("3.2.P.3.3_description_of_manufacturing_process_and_process_controls",
                   "Description of Manufacturing Process and Process Controls", []),
                  ("3.2.P.3.4_controls_of_critical_steps_and_intermediates",
                   "Controls of Critical Steps and Intermediates", []),
                  ("3.2.P.3.5_process_validation_and_or_evaluation",
                   "Process Validation and/or Evaluation", []),
              ]),
             ("3.2.P.4_control_of_excipients",
              "Control of Excipients", [
                  ("3.2.P.4.1_specifications",
                   "Specifications", []),
                  ("3.2.P.4.2_analytical_procedures",
                   "Analytical Procedures", []),
                  ("3.2.P.4.3_validation_of_analytical_procedures",
                   "Validation of Analytical Procedures", []),
                  ("3.2.P.4.4_justification_of_specifications",
                   "Justification of Specifications", []),
                  ("3.2.P.4.5_excipients_of_human_or_animal_origin",
                   "Excipients of Human or Animal Origin", []),
                  ("3.2.P.4.6_novel_excipients",
                   "Novel Excipients", []),
              ]),
             ("3.2.P.5_control_of_drug_product",
              "Control of Drug Product", [
                  ("3.2.P.5.1_specification",
                   "Specification", []),
                  ("3.2.P.5.2_analytical_procedures",
                   "Analytical Procedures", []),
                  ("3.2.P.5.3_validation_of_analytical_procedures",
                   "Validation of Analytical Procedures", []),
                  ("3.2.P.5.4_batch_analyses",
                   "Batch Analyses", []),
                  ("3.2.P.5.5_characterisation_of_impurities",
                   "Characterisation of Impurities", []),
                  ("3.2.P.5.6_justification_of_specification",
                   "Justification of Specification", []),
              ]),
             ("3.2.P.6_reference_standards_or_materials",
              "Reference Standards or Materials", []),
             ("3.2.P.7_container_closure_system",
              "Container Closure System", []),
             ("3.2.P.8_stability",
              "Stability", [
                  ("3.2.P.8.1_stability_summary_and_conclusions",
                   "Stability Summary and Conclusions", []),
                  ("3.2.P.8.2_post_approval_stability_protocol_and_stability_commitment",
                   "Post-Approval Stability Protocol and Stability Commitment", []),
                  ("3.2.P.8.3_stability_data",
                   "Stability Data", []),
              ]),
         ]),

        # 3.2.A — Appendices
        ("3.2.A_appendices",
         "Appendices", [
             ("3.2.A.1_facilities_and_equipment",
              "Facilities and Equipment", []),
             ("3.2.A.2_adventitious_agents_safety_evaluation",
              "Adventitious Agents Safety Evaluation", []),
             ("3.2.A.3_novel_excipients",
              "Novel Excipients", []),
         ]),

        # 3.2.R — Regional Information
        ("3.2.R_regional_information",
         "Regional Information", []),

        ("3.3_literature_references",
         "Literature References", []),
    ]),

    # ── Module 4 — Nonclinical Study Reports ─────────────────────────────────
    # Sub-subsections follow ICH M4S(R2) Table of Contents exactly.
    ("module4", "Nonclinical Study Reports", [
        ("4.1_table_of_contents",
         "Table of Contents", []),
        ("4.2_study_reports",
         "Study Reports", [
             # 4.2.1 Pharmacology
             ("4.2.1_pharmacology",
              "Pharmacology", [
                  ("4.2.1.1_primary_pharmacodynamics",
                   "Primary Pharmacodynamics", []),
                  ("4.2.1.2_secondary_pharmacodynamics",
                   "Secondary Pharmacodynamics", []),
                  ("4.2.1.3_safety_pharmacology",
                   "Safety Pharmacology", []),
                  ("4.2.1.4_pharmacodynamic_drug_interactions",
                   "Pharmacodynamic Drug Interactions", []),
              ]),
             # 4.2.2 Pharmacokinetics
             ("4.2.2_pharmacokinetics",
              "Pharmacokinetics", [
                  ("4.2.2.1_analytical_methods_and_validation_reports",
                   "Analytical Methods and Validation Reports", []),
                  ("4.2.2.2_absorption",
                   "Absorption", []),
                  ("4.2.2.3_distribution",
                   "Distribution", []),
                  ("4.2.2.4_metabolism",
                   "Metabolism", []),
                  ("4.2.2.5_excretion",
                   "Excretion", []),
                  ("4.2.2.6_pharmacokinetic_drug_interactions",
                   "Pharmacokinetic Drug Interactions (Nonclinical)", []),
                  ("4.2.2.7_other_pharmacokinetic_studies",
                   "Other Pharmacokinetic Studies", []),
              ]),
             # 4.2.3 Toxicology
             ("4.2.3_toxicology",
              "Toxicology", [
                  ("4.2.3.1_single_dose_toxicity",
                   "Single-Dose Toxicity", []),
                  ("4.2.3.2_repeat_dose_toxicity",
                   "Repeat-Dose Toxicity", []),
                  ("4.2.3.3_genotoxicity",
                   "Genotoxicity", [
                       ("4.2.3.3.1_in_vitro",
                        "In Vitro", []),
                       ("4.2.3.3.2_in_vivo",
                        "In Vivo", []),
                   ]),
                  ("4.2.3.4_carcinogenicity",
                   "Carcinogenicity", [
                       ("4.2.3.4.1_long_term_studies",
                        "Long-Term Studies", []),
                       ("4.2.3.4.2_short_or_medium_term_studies",
                        "Short- or Medium-Term Studies", []),
                       ("4.2.3.4.3_other_studies",
                        "Other Studies", []),
                   ]),
                  ("4.2.3.5_reproductive_and_developmental_toxicity",
                   "Reproductive and Developmental Toxicity", [
                       ("4.2.3.5.1_fertility_and_early_embryonic_development",
                        "Fertility and Early Embryonic Development", []),
                       ("4.2.3.5.2_embryo_fetal_development",
                        "Embryo-Fetal Development", []),
                       ("4.2.3.5.3_prenatal_and_postnatal_development",
                        "Prenatal and Postnatal Development", []),
                       ("4.2.3.5.4_studies_in_which_offspring_are_dosed",
                        "Studies in Which the Offspring (Juvenile Animals) Are Dosed",
                        []),
                   ]),
                  ("4.2.3.6_local_tolerance",
                   "Local Tolerance", []),
                  ("4.2.3.7_other_toxicity_studies",
                   "Other Toxicity Studies", [
                       ("4.2.3.7.1_antigenicity",
                        "Antigenicity", []),
                       ("4.2.3.7.2_immunotoxicity",
                        "Immunotoxicity", []),
                       ("4.2.3.7.3_mechanistic_studies",
                        "Mechanistic Studies", []),
                       ("4.2.3.7.4_dependence",
                        "Dependence", []),
                       ("4.2.3.7.5_metabolites",
                        "Metabolites", []),
                       ("4.2.3.7.6_impurities",
                        "Impurities", []),
                       ("4.2.3.7.7_other",
                        "Other", []),
                   ]),
              ]),
         ]),
        ("4.3_literature_references",
         "Literature References", []),
    ]),

    # ── Module 5 — Clinical Study Reports ────────────────────────────────────
    # The numbered sub-folders (5.3.x.y) are fixed ICH M4E categories.
    # Individual study PDFs are deposited inside the appropriate leaf folder
    # at submission time and are NOT part of this structural template.
    ("module5", "Clinical Study Reports", [
        ("5.1_table_of_contents",
         "Table of Contents", []),
        ("5.2_tabular_listing_of_all_clinical_studies",
         "Tabular Listing of All Clinical Studies", []),
        ("5.3_clinical_study_reports",
         "Clinical Study Reports", [
             # 5.3.1 Biopharmaceutics
             ("5.3.1_reports_of_biopharmaceutic_studies",
              "Reports of Biopharmaceutic Studies", [
                  ("5.3.1.1_bioavailability_study_reports",
                   "Bioavailability (BA) Study Reports", []),
                  ("5.3.1.2_comparative_ba_and_bioequivalence_study_reports",
                   "Comparative BA and Bioequivalence Study Reports", []),
                  ("5.3.1.3_in_vitro_in_vivo_correlation_study_reports",
                   "In Vitro–In Vivo Correlation Study Reports", []),
                  ("5.3.1.4_reports_of_bioanalytical_and_analytical_methods",
                   "Reports of Bioanalytical and Analytical Methods", []),
              ]),
             # 5.3.2 PK studies relevant to drug interactions and populations
             ("5.3.2_reports_of_studies_pertinent_to_pk_using_human_biomaterials",
              "Reports of Studies Pertinent to Pharmacokinetics Using Human Biomaterials", [
                  ("5.3.2.1_plasma_protein_binding_study_reports",
                   "Plasma Protein Binding Study Reports", []),
                  ("5.3.2.2_reports_of_hepatic_metabolism_and_drug_interaction_studies",
                   "Reports of Hepatic Metabolism and Drug Interaction Studies", []),
                  ("5.3.2.3_reports_of_studies_using_other_human_biomaterials",
                   "Reports of Studies Using Other Human Biomaterials", []),
              ]),
             # 5.3.3 Human PK study reports
             ("5.3.3_reports_of_human_pharmacokinetic_studies",
              "Reports of Human Pharmacokinetic (PK) Studies", [
                  ("5.3.3.1_healthy_subject_pk_and_initial_tolerability_study_reports",
                   "Healthy Subject PK and Initial Tolerability Study Reports", []),
                  ("5.3.3.2_patient_pk_and_initial_tolerability_study_reports",
                   "Patient PK and Initial Tolerability Study Reports", []),
                  ("5.3.3.3_intrinsic_factor_pk_study_reports",
                   "Intrinsic Factor PK Study Reports", []),
                  ("5.3.3.4_extrinsic_factor_pk_study_reports",
                   "Extrinsic Factor PK Study Reports", []),
                  ("5.3.3.5_population_pk_study_reports",
                   "Population PK Study Reports", []),
              ]),
             # 5.3.4 Human PD study reports
             ("5.3.4_reports_of_human_pharmacodynamic_studies",
              "Reports of Human Pharmacodynamic (PD) Studies", [
                  ("5.3.4.1_healthy_subject_pd_and_pk_pd_study_reports",
                   "Healthy Subject PD and PK/PD Study Reports", []),
                  ("5.3.4.2_patient_pd_and_pk_pd_study_reports",
                   "Patient PD and PK/PD Study Reports", []),
              ]),
             # 5.3.5 Efficacy and safety reports
             ("5.3.5_reports_of_efficacy_and_safety_studies",
              "Reports of Efficacy and Safety Studies", [
                  ("5.3.5.1_study_reports_of_controlled_clinical_studies_pertinent_to_the_claimed_indication",
                   "Study Reports of Controlled Clinical Studies Pertinent to the Claimed Indication",
                   []),
                  ("5.3.5.2_study_reports_of_uncontrolled_clinical_studies",
                   "Study Reports of Uncontrolled Clinical Studies", []),
                  ("5.3.5.3_reports_of_analyses_of_data_from_more_than_one_study",
                   "Reports of Analyses of Data from More Than One Study", []),
                  ("5.3.5.4_other_study_reports",
                   "Other Study Reports", []),
              ]),
             # 5.3.6 Post-marketing experience
             ("5.3.6_reports_of_post_marketing_experience",
              "Reports of Post-Marketing Experience", []),
             # 5.3.7 Case report forms / patient listings
             ("5.3.7_case_report_forms_and_individual_patient_listings",
              "Case Report Forms and Individual Patient Listings", []),
         ]),
        ("5.4_literature_references",
         "Literature References", []),
    ]),
]


# ── LangGraph state ───────────────────────────────────────────────────────────

# Structural gaps in the canonical template are code bugs; one rebuild pass is
# enough to self-heal any transient deserialisation edge-cases.
_MAX_REPAIR_RETRIES = 1  # cap repair_canonical loop to prevent infinite cycles on persistent bugs


class EnrichState(TypedDict):
    # TypedDict (not Pydantic BaseModel) because LangGraph merges node return dicts
    # directly into this state via shallow merge — Pydantic models are not compatible.
    ich_index_url: str          # base URL of the ICH4/index Cloud Run service; empty string skips enrichment
    reviewer_email: str | None  # approval email recipient; falls back to CTD_REVIEWER_EMAIL env var
    # Optional program-specific expansion — see expand_program_studies docstring.
    program_context: dict | None  # studies dict for Module 5 injection; None = static template only
    modules: list[dict]           # serialised CTDModule dicts (keys fixed, labels may be enriched)
    evaluation: dict | None       # serialised EvaluationResult; None until evaluate_completeness runs
    repair_retries: int           # counts repair_canonical invocations; capped at _MAX_REPAIR_RETRIES
    human_notified: bool          # True if approval email was sent successfully


# ── Query for label enrichment only ──────────────────────────────────────────

_LABEL_QUERY = (
    "Using the ICH M4(R4) guidelines, provide the official human-readable title "
    "for CTD {level} with identifier \"{key}\".\n\n"
    "Return ONLY a JSON object: {{\"label\": \"<official title>\"}}\n"
    "No prose, no markdown fences."
)


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _gcloud_token(audience: str = "") -> str:
    """Get an OIDC identity token for calling authenticated Cloud Run services.

    Tries gcloud CLI first (local dev) with --audiences so the token is scoped
    to the target service.  Inside Cloud Run / GCE falls back to the
    google-auth metadata-server approach so no CLI is needed.

    Always fetches a fresh token — callers should not cache the result across
    long-running operations because OIDC tokens expire after ~1 hour.
    """
    if audience:  # skip token fetch entirely if no audience provided (unauthenticated calls)
        try:
            r = subprocess.run(
                ["gcloud", "auth", "print-identity-token", f"--audiences={audience}"],
                capture_output=True, text=True,  # capture stdout/stderr; don't print to terminal
            )
            if r.returncode == 0:          # gcloud succeeded — use its token
                return r.stdout.strip()    # strip trailing newline from gcloud output
        except FileNotFoundError:
            pass  # gcloud not installed (e.g. inside a container) — fall through to metadata server
        try:
            import google.auth.transport.requests  # lazy import — only needed inside Cloud Run/GCE
            import google.oauth2.id_token
            return google.oauth2.id_token.fetch_id_token(
                google.auth.transport.requests.Request(), audience  # uses metadata server inside GCP
            )
        except Exception:
            pass  # metadata server unreachable — return empty token below
    return ""  # no token available; caller will proceed without Authorization header


def _post_query(url: str, question: str) -> str:
    """POST a question to the ICH index with a fresh token on each call."""
    token = _gcloud_token(audience=url)        # fresh OIDC token scoped to this URL
    headers = {"Content-Type": "application/json"}  # ICH index expects JSON body
    if token:
        headers["Authorization"] = f"Bearer {token}"  # attach token only when available
    resp = requests.post(
        f"{url}/index/query",   # /index/query is the only endpoint called from structure.py
        headers=headers,
        json={"question": question},  # body schema expected by the ICH index service
        timeout=60,  # 60s — enrichment calls can be slow; subsection queries are skipped entirely
    )
    resp.raise_for_status()  # raise HTTPError on 4xx/5xx so caller's except block catches it
    return resp.json().get("answer", "")  # index returns {"answer": "<label text>"}


def _extract_json_object(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?|```", "", text).strip()  # strip markdown code fences the LLM may add
    start, end = text.find("{"), text.rfind("}") + 1       # find outermost JSON object boundaries
    if start == -1 or end == 0:  # no braces found — LLM returned plain text or empty string
        return None
    try:
        return json.loads(text[start:end])  # parse only the JSON object, ignoring surrounding prose
    except Exception:
        return None  # malformed JSON — caller treats missing label as no-op and keeps canonical


# ── Canonical tree builder ────────────────────────────────────────────────────

def _build_subsection(raw: tuple) -> "CTDSubsection":
    """Recursively build a CTDSubsection from a (key, label, children) tuple."""
    key, label, children = raw  # unpack the 3-element canonical tuple
    return CTDSubsection(
        key=key,
        label=label,
        sub_subsections=[_build_subsection(c) for c in children],  # recurse for each child tuple
    )


def _build_canonical_modules() -> list[CTDModule]:
    """Instantiate CTDModule objects directly from _ICH_M4_CANONICAL (4-level)."""
    modules: list[CTDModule] = []  # accumulates all 5 CTDModule objects
    for mod_key, mod_label, sections_raw in _ICH_M4_CANONICAL:  # iterate over 5 top-level modules
        sections: list[CTDSection] = []  # accumulates CTDSection objects for this module
        for sec_key, sec_label, subs_raw in sections_raw:  # iterate over sections within this module
            subsections = [_build_subsection(s) for s in subs_raw]  # build depth-3+ tree recursively
            sections.append(
                CTDSection(key=sec_key, label=sec_label, subsections=subsections)
            )
        modules.append(CTDModule(key=mod_key, label=mod_label, sections=sections))
    return modules


# ── Evaluation helper ─────────────────────────────────────────────────────────

def _collect_canonical_paths() -> set[str]:
    """Return every expected folder path from _ICH_M4_CANONICAL for validation."""
    paths: set[str] = set()  # stores slash-joined paths like 'module3/3.2.S_drug_substance'

    def _recurse(prefix: str, children: list) -> None:
        for key, _label, grandchildren in children:  # _label unused — only path matters here
            p = f"{prefix}/{key}"  # build slash-joined path from root to this node
            paths.add(p)
            _recurse(p, grandchildren)  # recurse into sub-subsections

    for mod_key, _mod_label, sections in _ICH_M4_CANONICAL:
        paths.add(mod_key)         # add top-level module key (e.g. 'module3')
        _recurse(mod_key, sections)  # add all descendant paths under this module
    return paths


def _ich_number_prefix(key: str) -> str:
    """Extract the ICH numeric/alphanumeric prefix of a folder key.

    Returns the substring before the first ``_`` only when it contains a dot
    (all ICH section identifiers contain at least one dot, e.g. ``3.2.S.1``).
    Keys without a dot prefix — e.g. ``module3``, ``study_001_phase3`` —
    return ``""`` and are skipped in numbering-consistency checks.

    Examples::

        '3.2.S.1_general_information' -> '3.2.S.1'
        '4.2.3.3.1_in_vitro'         -> '4.2.3.3.1'
        'module3'                     -> ''
        'study_001_phase3_t2d'        -> ''  # program-specific, no ICH number
    """
    idx = key.find("_")           # find first underscore separating ICH number from label words
    if idx == -1:
        return ""                  # no underscore — key has no ICH number prefix
    prefix = key[:idx]             # e.g. '3.2.S.1' from '3.2.S.1_general_information'
    return prefix if "." in prefix else ""  # reject 'module3', 'study001' — no dot = no ICH number


def _prefix_is_child(parent_key: str, child_key: str) -> bool:
    """Return True if *child_key*'s ICH number extends *parent_key*'s number.

    Valid: child prefix starts with parent prefix + "."
    Skipped (returns True) when either key has no ICH numeric prefix.
    """
    parent_pfx = _ich_number_prefix(parent_key)  # e.g. '3.2.S' from '3.2.S_drug_substance'
    child_pfx = _ich_number_prefix(child_key)    # e.g. '3.2.S.1' from '3.2.S.1_general_information'
    if not parent_pfx or not child_pfx:
        return True  # one side has no ICH number (module key or program-specific) — skip check
    return child_pfx.startswith(parent_pfx + ".")  # valid: '3.2.S.1'.startswith('3.2.S.') → True


def _evaluate(modules: list[CTDModule]) -> EvaluationResult:
    """Validate the assembled tree against the canonical ICH M4(R4) template.

    Checks performed:
      1. Module presence      — all five canonical modules must appear.
      2. Empty modules        — any module with zero sections is flagged.
      3. Duplicate keys       — duplicate section/subsection/sub-subsection
                                keys within the same parent are errors.
      4. ICH prefix chain     — every child's ICH number must extend its
                                parent's number (e.g. 3.2.S.1 is valid under
                                3.2.S; 4.2.1 under 3.2.S is a mismatch).
      5. Canonical completeness — every path in _ICH_M4_CANONICAL must be
                                  present.  Program-specific additions are
                                  allowed but cannot remove canonical paths.
    """
    issues: list[EvaluationIssue] = []  # collects all validation failures found during the walk

    # ── 1. Module presence ────────────────────────────────────────────────────
    canonical_keys = {mod_key for mod_key, *_ in _ICH_M4_CANONICAL}  # expected: {'module1'..'module5'}
    present_keys = {m.key for m in modules}                           # actual keys in assembled tree
    for key in canonical_keys - present_keys:  # any module in canonical but absent from tree
        issues.append(EvaluationIssue(
            path=key, level="module",
            reason="module missing from canonical template"))

    # ── Walk the tree collecting paths and running checks ─────────────────────
    actual_paths: set[str] = set()  # all slash-joined paths seen in the assembled tree

    def _walk_sub(prefix: str, parent_key: str, sub: CTDSubsection) -> None:
        sp = f"{prefix}{sub.key}"  # full slash-joined path to this subsection
        actual_paths.add(sp)       # record so check 5 can compare against canonical
        # ── 4. Prefix chain check ─────────────────────────────────────────────
        if not _prefix_is_child(parent_key, sub.key):  # e.g. '4.2.1' under '3.2.S' is invalid
            issues.append(EvaluationIssue(
                path=sp, level="subsection",
                reason=(
                    f"ICH prefix mismatch: '{sub.key}' is not a numbered "
                    f"child of '{parent_key}'"
                )))
        seen: set[str] = set()  # tracks child keys within this subsection to catch duplicates
        for child in sub.sub_subsections:
            if child.key in seen:  # duplicate key at depth 4
                issues.append(EvaluationIssue(
                    path=f"{sp}/{child.key}", level="sub_subsection",
                    reason=f"duplicate sub-subsection key '{child.key}'"))
            seen.add(child.key)
            _walk_sub(f"{sp}/", sub.key, child)  # recurse: sub becomes parent for its children

    for mod in modules:
        actual_paths.add(mod.key)  # record top-level module path (e.g. 'module3')
        # ── 2. Empty module ───────────────────────────────────────────────────
        if not mod.sections:  # a module with no sections is a structural bug
            issues.append(EvaluationIssue(
                path=mod.key, level="module",
                reason="module has zero sections"))
            continue  # skip section walk — nothing to traverse
        seen_sec: set[str] = set()  # tracks section keys within this module
        for sec in mod.sections:
            sec_path = f"{mod.key}/{sec.key}"  # e.g. 'module2/2.5_clinical_overview'
            actual_paths.add(sec_path)
            # ── 3a. Duplicate section ─────────────────────────────────────────
            if sec.key in seen_sec:  # same section key appears twice under this module
                issues.append(EvaluationIssue(
                    path=sec_path, level="section",
                    reason=f"duplicate section key '{sec.key}'"))
            seen_sec.add(sec.key)
            seen_sub: set[str] = set()  # tracks subsection keys within this section
            for sub in sec.subsections:
                sub_path = f"{sec_path}/{sub.key}"  # e.g. '…/2.5_clinical_overview/2.5.1_…'
                # ── 3b. Duplicate subsection ──────────────────────────────────
                if sub.key in seen_sub:  # same subsection key appears twice under this section
                    issues.append(EvaluationIssue(
                        path=sub_path, level="subsection",
                        reason=f"duplicate subsection key '{sub.key}'"))
                seen_sub.add(sub.key)
                # Recurse — _walk_sub validates sub.key against sec.key (its parent)
                _walk_sub(f"{sec_path}/", sec.key, sub)

    # ── 5. Canonical completeness ─────────────────────────────────────────────
    canonical_paths = _collect_canonical_paths()         # full expected path set from _ICH_M4_CANONICAL
    for path in sorted(canonical_paths - actual_paths):  # paths expected but not found in assembled tree
        issues.append(EvaluationIssue(
            path=path, level="section",
            reason="canonical ICH M4(R4) path missing from assembled structure"))

    passed = not issues  # True only when zero issues were found
    if passed:
        n_sec = sum(len(m.sections) for m in modules)                          # total section count
        n_sub = sum(len(s.subsections) for m in modules for s in m.sections)  # total subsection count
        n_subsub = sum(
            len(sub.sub_subsections)
            for m in modules for s in m.sections for sub in s.subsections
        )  # total sub-subsection count
        summary = (
            f"Structure complete: {len(modules)} modules, {n_sec} sections, "
            f"{n_sub} subsections, {n_subsub} sub-subsections"
            f" — ready for human review."
        )
    else:
        lines = [f"{len(issues)} issue(s) found:"]  # header line for the issues report
        for iss in issues:
            lines.append(f"  [{iss.level.upper()}] {iss.path} — {iss.reason}")
        summary = "\n".join(lines)  # multi-line string shown to the reviewer
    return EvaluationResult(passed=passed, issues=issues, summary=summary)


# ── Email helpers ─────────────────────────────────────────────────────────────

def _format_email_body(modules: list[CTDModule], evaluation: EvaluationResult) -> str:
    sep = "=" * 60                                  # visual separator line in plain-text email
    status = "PASSED" if evaluation.passed else "ISSUES FOUND"  # one-word verdict for email subject line
    lines = [
        "ICH CTD Canonical Folder Structure — Approval Required",
        sep, "",
        f"Evaluation : {status}",
        evaluation.summary, "",
        "── Proposed folder structure ──────────────────────────────────────",
    ]
    flagged = {i.path: i.reason for i in evaluation.issues}  # path → reason lookup for inline annotation
    for mod in modules:
        ann = f"  ◄ {flagged[mod.key]}" if mod.key in flagged else ""  # annotate flagged modules
        lines.append(f"\n{mod.key}/  {mod.label}{ann}")
        for sec in mod.sections:
            sec_path = f"{mod.key}/{sec.key}"                              # build path for lookup
            ann = f"  ◄ {flagged[sec_path]}" if sec_path in flagged else ""  # annotate flagged sections
            lines.append(f"  {sec.key}/  {sec.label}{ann}")
            for sub in sec.subsections:
                sub_path = f"{mod.key}/{sec.key}/{sub.key}"                     # depth-3 path
                ann = f"  ◄ {flagged[sub_path]}" if sub_path in flagged else ""  # annotate flagged subsections
                lines.append(f"    {sub.key}/  {sub.label}{ann}")
                for subsub in sub.sub_subsections:
                    ss_path = f"{mod.key}/{sec.key}/{sub.key}/{subsub.key}"       # depth-4 path
                    ann = f"  ◄ {flagged[ss_path]}" if ss_path in flagged else ""  # annotate flagged sub-subsections
                    lines.append(f"      {subsub.key}/  {subsub.label}{ann}")
    lines += [
        "", sep,
        "Reply APPROVE or REJECT to this email, or confirm in the terminal.",
    ]
    return "\n".join(lines)  # single string with newlines — sent as plain/text MIME part


def _send_approval_email(to_address: str, subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")  # default to Gmail; override for SendGrid etc.
    port = int(os.environ.get("SMTP_PORT", "587"))         # 587 = STARTTLS; use 465 for SSL
    user = os.environ["SMTP_USER"]                         # raises KeyError if not set — caught by caller
    password = os.environ["SMTP_PASSWORD"]                 # raises KeyError if not set — caught by caller
    msg = MIMEMultipart("alternative")  # multipart/alternative allows plain + HTML parts
    msg["Subject"] = subject
    msg["From"] = user         # sender address shown in email client
    msg["To"] = to_address     # reviewer's email address
    msg.attach(MIMEText(body, "plain"))  # only plain text for now — no HTML part
    with smtplib.SMTP(host, port) as smtp:  # opens connection; context manager ensures close
        smtp.ehlo()      # identify client to server (required before STARTTLS)
        smtp.starttls()  # upgrade to TLS — must be called before login
        smtp.login(user, password)              # authenticate with SMTP credentials
        smtp.sendmail(user, to_address, msg.as_string())  # send the fully-assembled message


# ── Graph nodes ───────────────────────────────────────────────────────────────

def load_canonical(state: EnrichState) -> dict:
    """Build the full module tree from the hardcoded canonical template."""
    # state is intentionally unused — this is the seed node; nothing upstream has produced state yet.
    # LangGraph requires all nodes to accept state as their first argument regardless.
    modules = _build_canonical_modules()  # instantiate all 5 CTDModule objects from _ICH_M4_CANONICAL
    return {"modules": [m.model_dump() for m in modules]}  # seeds state["modules"] for downstream nodes


def enrich_labels(state: EnrichState) -> dict:
    """
    Optionally query the ICH index to refine human-readable labels.
    Keys are NEVER changed — only labels may be updated.
    If the index is unreachable the canonical labels are kept as-is.
    """
    url = state.get("ich_index_url", "")  # empty string means enrichment is disabled
    if not url:
        return {}  # return empty dict — LangGraph merges nothing, state["modules"] unchanged

    modules = [CTDModule(**m) for m in state["modules"]]  # deserialise from state dicts to Pydantic models
    enriched: list[dict] = []  # accumulates enriched module dicts to return

    for mod in modules:
        try:
            ans = _post_query(url, _LABEL_QUERY.format(level="module", key=mod.key))  # query ICH index for module label
            data = _extract_json_object(ans)    # parse {"label": "..."} from response
            if data and data.get("label"):      # only update if index returned a non-empty label
                mod = mod.model_copy(update={"label": data["label"]})  # Pydantic immutable update — key is never changed
        except Exception:
            pass  # network error or bad JSON — keep canonical label as-is

        enriched_sections: list[CTDSection] = []  # accumulates enriched sections for this module
        for sec in mod.sections:
            try:
                ans = _post_query(url, _LABEL_QUERY.format(level="section", key=sec.key))  # query ICH index for section label
                data = _extract_json_object(ans)
                if data and data.get("label"):
                    sec = sec.model_copy(update={"label": data["label"]})  # update label only; key unchanged
            except Exception:
                pass  # keep canonical label on any failure
            enriched_sections.append(sec)

        mod = mod.model_copy(update={"sections": enriched_sections})  # attach enriched sections to module
        enriched.append(mod.model_dump())  # serialise back to dict for state merge

    return {"modules": enriched}  # LangGraph merges this into state, replacing state["modules"]


def evaluate_completeness(state: EnrichState) -> dict:
    modules = [CTDModule(**m) for m in state["modules"]]  # deserialise enriched module dicts to Pydantic models
    evaluation = _evaluate(modules)                         # run all 5 structural checks
    return {"evaluation": evaluation.model_dump()}          # seeds state["evaluation"] for after_evaluate routing


def notify_human(state: EnrichState) -> dict:
    reviewer = state.get("reviewer_email") or os.environ.get("CTD_REVIEWER_EMAIL")  # state value overrides env var
    if not reviewer:  # no email configured — skip silently
        print("[ctd_structure] No reviewer email — skipping notification.")
        return {"human_notified": False}  # update state so callers know email was skipped

    modules = [CTDModule(**m) for m in state["modules"]]  # deserialise for email body formatting
    evaluation = (
        EvaluationResult(**state["evaluation"])  # deserialise evaluation result from state
        if state.get("evaluation")
        else EvaluationResult(passed=False, summary="Evaluation not available.")  # fallback if node was skipped
    )
    subject = "[CTD Approval Required] ICH M4(R4) Canonical Structure"  # fixed subject for all approval emails
    body = _format_email_body(modules, evaluation)  # build plain-text email body with folder tree
    try:
        _send_approval_email(reviewer, subject, body)  # raises KeyError on missing SMTP_USER/SMTP_PASSWORD
        print(f"[ctd_structure] Approval email sent → {reviewer}")
        return {"human_notified": True}   # email delivered successfully
    except KeyError as exc:
        print(f"[ctd_structure] Email skipped — missing env var: {exc}")  # SMTP_USER or SMTP_PASSWORD not set
    except Exception as exc:
        print(f"[ctd_structure] Email failed: {exc}")  # network error, auth failure, etc.
    return {"human_notified": False}  # email was not sent — caller can check this flag


# ── Dynamic expansion node ────────────────────────────────────────────────────

def expand_program_studies(state: EnrichState) -> dict:
    """Inject program-specific study placeholder sub-folders into Module 5.

    Reads ``state["program_context"]["studies"]``, which maps any existing
    5.3.x.y sub-subsection key to a list of study entries::

        {
            "studies": {
                "5.3.1.1_bioavailability_study_reports": [
                    {"key": "study_001_ba_fasting",  "label": "Study 001 BA Fasting"},
                    {"key": "study_002_ba_fed_state", "label": "Study 002 BA Fed State"},
                ],
                "5.3.5.1_controlled_clinical_studies...": [
                    {"key": "study_ph3_001", "label": "Phase 3 Study 001 Title"},
                ],
            }
        }

    Each entry becomes a child CTDSubsection (5th-level GCS folder) under its
    parent 4th-level ICH category folder.  Keys must be folder-safe strings.
    This is a NO-OP when ``program_context`` is absent or ``studies`` is empty.
    """
    ctx = state.get("program_context") or {}               # guard against None
    study_map: dict[str, list[dict]] = ctx.get("studies", {})  # maps 5.3.x.y key → list of study entries
    if not study_map:
        return {}  # no studies to inject — leave state["modules"] unchanged

    modules = [CTDModule(**m) for m in state["modules"]]  # deserialise from state dicts
    updated = False  # flag: True if at least one study sub-folder was injected

    def _inject(sub: CTDSubsection) -> CTDSubsection:
        nonlocal updated
        new_children = [_inject(c) for c in sub.sub_subsections]  # recurse to find matching leaf nodes
        for s in study_map.get(sub.key, []):  # look up this subsection's key in the study map
            if not s.get("key"):
                continue  # skip malformed entries with no key
            new_children.append(CTDSubsection(
                key=s["key"],                      # program-specific folder key, e.g. 'study_001_ba'
                label=s.get("label", s["key"]),    # human-readable label; falls back to key if missing
                sub_subsections=[],                # studies are leaf nodes — no children
            ))
            updated = True
        if new_children != list(sub.sub_subsections):  # only rebuild if something changed
            return sub.model_copy(update={"sub_subsections": new_children})
        return sub  # unchanged — return original object

    result_modules: list[CTDModule] = []
    for mod in modules:
        if mod.key != "module5":       # expansion only applies to Module 5 (clinical study reports)
            result_modules.append(mod)
            continue
        new_sections = [
            sec.model_copy(update={"subsections": [_inject(s) for s in sec.subsections]})
            for sec in mod.sections    # walk every section in module5 to find injection targets
        ]
        result_modules.append(mod.model_copy(update={"sections": new_sections}))

    if updated:  # only update state if at least one study was injected
        return {"modules": [m.model_dump() for m in result_modules]}
    return {}  # nothing changed — return empty dict so LangGraph skips the merge


# ── Recovery node ─────────────────────────────────────────────────────────────

def repair_canonical(state: EnrichState) -> dict:
    """Rebuild any canonical module/section that is missing or empty.

    Called by the LangGraph repair loop when ``evaluate_completeness`` reports
    module-level or canonical-completeness failures.  Since the structure is
    fully deterministic, a rebuild from ``_ICH_M4_CANONICAL`` always produces
    a correct result.  Program-specific additions (from expand_program_studies)
    in non-flagged modules are preserved.

    ``repair_retries`` is incremented here so ``after_evaluate`` can cap the
    loop at ``_MAX_REPAIR_RETRIES`` and avoid infinite cycles.
    """
    evaluation = EvaluationResult(**state["evaluation"])      # deserialise to access issue list
    canonical_modules = _build_canonical_modules()              # fresh canonical tree — always correct
    canonical_by_key = {m.key: m for m in canonical_modules}   # lookup: module key → CTDModule

    flagged_mods: set[str] = set()  # module keys that have at least one structural issue
    for issue in evaluation.issues:
        flagged_mods.add(issue.path.split("/")[0])  # first path segment is always the module key

    repaired: list[dict] = []
    for mod_data in state["modules"]:
        mod_key = mod_data["key"]
        if mod_key in flagged_mods and mod_key in canonical_by_key:
            print(f"[ctd_structure] repair: rebuilt canonical {mod_key}")  # log which module was repaired
            repaired.append(canonical_by_key[mod_key].model_dump())  # replace with clean canonical copy
        else:
            repaired.append(mod_data)  # unflagged module — preserve as-is (keeps program-specific additions)

    present = {m["key"] for m in repaired}  # keys already in the repaired list
    for mod in canonical_modules:
        if mod.key not in present:  # module was entirely missing (not just flagged)
            print(f"[ctd_structure] repair: added missing {mod.key}")
            repaired.append(mod.model_dump())  # append the missing canonical module

    return {
        "modules": repaired,                                    # patched module list
        "repair_retries": state.get("repair_retries", 0) + 1,  # increment counter so after_evaluate can cap the loop
    }


# ── Routing ────────────────────────────────────────────────────────────────────

def after_evaluate(state: EnrichState) -> str:
    """Route to repair loop or human notification after evaluation.

    Structural issues (missing modules, canonical paths absent) trigger
    ``repair_canonical`` — but only up to ``_MAX_REPAIR_RETRIES`` times to
    prevent infinite loops.  Duplicate/prefix issues are advisory and go
    directly to the reviewer.
    """
    evaluation = EvaluationResult(**state["evaluation"])  # deserialise to inspect issue list
    structural = [
        i for i in evaluation.issues
        if i.level == "module"                      # missing or empty module
        or i.reason.startswith("canonical ICH")     # canonical path absent from assembled tree
    ]  # duplicate/prefix issues are advisory — route straight to reviewer without repair
    if structural and state.get("repair_retries", 0) < _MAX_REPAIR_RETRIES:
        return "repair_canonical"  # trigger repair loop — will re-run evaluate_completeness after
    return "notify_human"  # no structural issues, or repair retries exhausted — proceed to reviewer


# ── Graph assembly ────────────────────────────────────────────────────────────

def _build_graph():
    g = StateGraph(EnrichState)  # creates a graph whose nodes share EnrichState as their state type

    # ── Register all nodes ────────────────────────────────────────────────────
    g.add_node("load_canonical",         load_canonical)          # seeds state["modules"] from hardcoded template
    g.add_node("expand_program_studies", expand_program_studies)  # injects program-specific study folders into module5
    g.add_node("enrich_labels",          enrich_labels)           # queries ICH index to improve module/section labels
    g.add_node("evaluate_completeness",  evaluate_completeness)   # validates tree against canonical; sets state["evaluation"]
    g.add_node("repair_canonical",       repair_canonical)        # rebuilds flagged modules from canonical template
    g.add_node("notify_human",           notify_human)            # sends approval email to reviewer

    # ── Wire edges ────────────────────────────────────────────────────────────
    g.set_entry_point("load_canonical")                                    # pipeline always starts here
    g.add_edge("load_canonical",          "expand_program_studies")        # always expand after seeding
    g.add_edge("expand_program_studies",  "enrich_labels")                 # always enrich after expansion
    g.add_edge("enrich_labels",           "evaluate_completeness")         # always evaluate after enrichment
    g.add_conditional_edges("evaluate_completeness", after_evaluate)       # routes to repair_canonical or notify_human
    g.add_edge("repair_canonical",        "evaluate_completeness")         # re-evaluate after repair (loop)
    g.add_edge("notify_human",            END)                             # terminal node — pipeline ends here
    return g.compile()  # compile locks the graph and returns a runnable Pregel executor


_graph = _build_graph()


# ── Public entry points ───────────────────────────────────────────────────────

def refine_from_feedback(
    ich_index_url: str,
    feedback: str,
    current_output: "CTDStructureOutput",
) -> "tuple[CTDStructureOutput, EvaluationResult]":
    """
    Re-build from the canonical template and re-evaluate.

    The ``feedback`` string is logged for human reviewers but does not alter keys.
    Label enrichment is re-run so the ICH index can refine titles.

    Args:
        ich_index_url:  Base URL of the deployed ICH4/index Cloud Run service.
        feedback:       Free-text user feedback (logged, not used to change keys).
        current_output: Previous output (kept for API compatibility).

    Returns:
        (updated_CTDStructureOutput, EvaluationResult)
    """
    print(f"[ctd_structure] refine_from_feedback: '{feedback[:120]}'")  # logs first 120 chars only; feedback is NOT forwarded to the pipeline
    # BUG (Item 7): feedback is silently discarded here — EnrichState has no feedback field,
    # so there is no mechanism to route it into the LangGraph pipeline.
    # current_output is also ignored — this is a full rebuild, not a refinement.
    # The result is identical to calling extract_from_ich_index directly.
    return extract_from_ich_index(ich_index_url, reviewer_email=None)  # full rebuild from canonical; feedback has no effect


def extract_from_ich_index(
    ich_index_url: str,
    reviewer_email: str | None = None,
    program_context: dict | None = None,
) -> tuple[CTDStructureOutput, EvaluationResult]:
    """Build the canonical ICH M4(R4) CTD folder structure.

    LangGraph pipeline::

        load_canonical
          -> expand_program_studies   (no-op when program_context is None)
          -> enrich_labels            (no-op when ich_index_url is empty)
          -> evaluate_completeness
          -> [repair_canonical ->]*   (triggered only on structural failures)
          -> notify_human

    Args:
        ich_index_url:   Base URL of the deployed ICH4/index Cloud Run service.
                         Pass ``""`` or ``None`` to skip label enrichment.
        reviewer_email:  Email address for the approval request.  Falls back
                         to the ``CTD_REVIEWER_EMAIL`` env var.
        program_context: Optional dict for program-specific Module 5 expansion.
                         Format::

                           {
                               "studies": {
                                   "<5.3.x.y_leaf_key>": [
                                       {"key": "study_001", "label": "Title"},
                                   ],
                               }
                           }

                         Pass ``None`` for the static ICH M4 template only.

    Returns:
        (CTDStructureOutput, EvaluationResult)
    """
    result = _graph.invoke({  # runs the full pipeline synchronously; blocks until notify_human completes
        "ich_index_url": ich_index_url or "",  # normalise None to empty string for enrich_labels guard
        "reviewer_email": reviewer_email,       # passed through state to notify_human
        "program_context": program_context,     # passed through state to expand_program_studies
        "modules": [],        # empty list — load_canonical will populate this
        "evaluation": None,   # None until evaluate_completeness runs
        "repair_retries": 0,  # counter starts at zero; capped at _MAX_REPAIR_RETRIES
        "human_notified": False,  # updated by notify_human
    })

    modules = [CTDModule(**m) for m in result["modules"]]  # deserialise final module list from state
    evaluation = (
        EvaluationResult(**result["evaluation"])  # deserialise evaluation result
        if result.get("evaluation")
        else EvaluationResult(passed=False, summary="Evaluation did not run.")  # defensive fallback
    )
    return CTDStructureOutput(modules=modules, evaluation=evaluation), evaluation  # return both for API consumers
