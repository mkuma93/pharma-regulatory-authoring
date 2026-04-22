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
from __future__ import annotations

import json
import os
import re
import smtplib
import subprocess
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


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
    path: str
    level: str   # "module" | "section" | "subsection"
    reason: str


class EvaluationResult(BaseModel):
    passed: bool
    issues: list[EvaluationIssue] = Field(default_factory=list)
    summary: str = ""


EvaluationResult.model_rebuild()


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
        paths: list[str] = []

        def _recurse(prefix: str, sub: "CTDSubsection") -> None:
            p = f"{prefix}{sub.key}/"
            paths.append(p)
            for child in sub.sub_subsections:
                _recurse(p, child)

        for module in self.modules:
            mp = f"ctd/{module.key}/"
            paths.append(mp)
            for section in module.sections:
                sp = f"{mp}{section.key}/"
                paths.append(sp)
                for sub in section.subsections:
                    _recurse(sp, sub)
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
_MAX_REPAIR_RETRIES = 1


class EnrichState(TypedDict):
    ich_index_url: str
    reviewer_email: str | None
    # Optional program-specific expansion — see expand_program_studies docstring.
    program_context: dict | None
    modules: list[dict]  # serialised CTDModule dicts (keys fixed, labels may be enriched)
    evaluation: dict | None
    repair_retries: int  # counts repair_canonical invocations; capped at _MAX_REPAIR_RETRIES
    human_notified: bool


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
    if audience:
        try:
            r = subprocess.run(
                ["gcloud", "auth", "print-identity-token", f"--audiences={audience}"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except FileNotFoundError:
            pass  # gcloud not installed (e.g. inside a container)
        try:
            import google.auth.transport.requests
            import google.oauth2.id_token
            return google.oauth2.id_token.fetch_id_token(
                google.auth.transport.requests.Request(), audience
            )
        except Exception:
            pass
    return ""


def _post_query(url: str, question: str) -> str:
    """POST a question to the ICH index with a fresh token on each call."""
    token = _gcloud_token(audience=url)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(
        f"{url}/index/query",
        headers=headers,
        json={"question": question},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("answer", "")


def _extract_json_object(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    start, end = text.find("{"), text.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    try:
        return json.loads(text[start:end])
    except Exception:
        return None


# ── Canonical tree builder ────────────────────────────────────────────────────

def _build_subsection(raw: tuple) -> "CTDSubsection":
    """Recursively build a CTDSubsection from a (key, label, children) tuple."""
    key, label, children = raw
    return CTDSubsection(
        key=key,
        label=label,
        sub_subsections=[_build_subsection(c) for c in children],
    )


def _build_canonical_modules() -> list[CTDModule]:
    """Instantiate CTDModule objects directly from _ICH_M4_CANONICAL (4-level)."""
    modules: list[CTDModule] = []
    for mod_key, mod_label, sections_raw in _ICH_M4_CANONICAL:
        sections: list[CTDSection] = []
        for sec_key, sec_label, subs_raw in sections_raw:
            subsections = [_build_subsection(s) for s in subs_raw]
            sections.append(
                CTDSection(key=sec_key, label=sec_label, subsections=subsections)
            )
        modules.append(CTDModule(key=mod_key, label=mod_label, sections=sections))
    return modules


# ── Evaluation helper ─────────────────────────────────────────────────────────

def _collect_canonical_paths() -> set[str]:
    """Return every expected folder path from _ICH_M4_CANONICAL for validation."""
    paths: set[str] = set()

    def _recurse(prefix: str, children: list) -> None:
        for key, _label, grandchildren in children:
            p = f"{prefix}/{key}"
            paths.add(p)
            _recurse(p, grandchildren)

    for mod_key, _mod_label, sections in _ICH_M4_CANONICAL:
        paths.add(mod_key)
        _recurse(mod_key, sections)
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
    idx = key.find("_")
    if idx == -1:
        return ""
    prefix = key[:idx]
    return prefix if "." in prefix else ""


def _prefix_is_child(parent_key: str, child_key: str) -> bool:
    """Return True if *child_key*'s ICH number extends *parent_key*'s number.

    Valid: child prefix starts with parent prefix + "."
    Skipped (returns True) when either key has no ICH numeric prefix.
    """
    parent_pfx = _ich_number_prefix(parent_key)
    child_pfx = _ich_number_prefix(child_key)
    if not parent_pfx or not child_pfx:
        return True  # no ICH number to validate
    return child_pfx.startswith(parent_pfx + ".")


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
    issues: list[EvaluationIssue] = []

    # ── 1. Module presence ────────────────────────────────────────────────────
    canonical_keys = {mod_key for mod_key, *_ in _ICH_M4_CANONICAL}
    present_keys = {m.key for m in modules}
    for key in canonical_keys - present_keys:
        issues.append(EvaluationIssue(
            path=key, level="module",
            reason="module missing from canonical template"))

    # ── Walk the tree collecting paths and running checks ─────────────────────
    actual_paths: set[str] = set()

    def _walk_sub(prefix: str, parent_key: str, sub: CTDSubsection) -> None:
        sp = f"{prefix}{sub.key}"
        actual_paths.add(sp)
        # ── 4. Prefix chain check ─────────────────────────────────────────────
        if not _prefix_is_child(parent_key, sub.key):
            issues.append(EvaluationIssue(
                path=sp, level="subsection",
                reason=(
                    f"ICH prefix mismatch: '{sub.key}' is not a numbered "
                    f"child of '{parent_key}'"
                )))
        seen: set[str] = set()
        for child in sub.sub_subsections:
            if child.key in seen:
                issues.append(EvaluationIssue(
                    path=f"{sp}/{child.key}", level="sub_subsection",
                    reason=f"duplicate sub-subsection key '{child.key}'"))
            seen.add(child.key)
            _walk_sub(f"{sp}/", sub.key, child)

    for mod in modules:
        actual_paths.add(mod.key)
        # ── 2. Empty module ───────────────────────────────────────────────────
        if not mod.sections:
            issues.append(EvaluationIssue(
                path=mod.key, level="module",
                reason="module has zero sections"))
            continue
        seen_sec: set[str] = set()
        for sec in mod.sections:
            sec_path = f"{mod.key}/{sec.key}"
            actual_paths.add(sec_path)
            # ── 3a. Duplicate section ─────────────────────────────────────────
            if sec.key in seen_sec:
                issues.append(EvaluationIssue(
                    path=sec_path, level="section",
                    reason=f"duplicate section key '{sec.key}'"))
            seen_sec.add(sec.key)
            seen_sub: set[str] = set()
            for sub in sec.subsections:
                sub_path = f"{sec_path}/{sub.key}"
                # ── 3b. Duplicate subsection ──────────────────────────────────
                if sub.key in seen_sub:
                    issues.append(EvaluationIssue(
                        path=sub_path, level="subsection",
                        reason=f"duplicate subsection key '{sub.key}'"))
                seen_sub.add(sub.key)
                # Recurse — _walk_sub validates sub.key against sec.key (its parent)
                _walk_sub(f"{sec_path}/", sec.key, sub)

    # ── 5. Canonical completeness ─────────────────────────────────────────────
    canonical_paths = _collect_canonical_paths()
    for path in sorted(canonical_paths - actual_paths):
        issues.append(EvaluationIssue(
            path=path, level="section",
            reason="canonical ICH M4(R4) path missing from assembled structure"))

    passed = not issues
    if passed:
        n_sec = sum(len(m.sections) for m in modules)
        n_sub = sum(len(s.subsections) for m in modules for s in m.sections)
        n_subsub = sum(
            len(sub.sub_subsections)
            for m in modules for s in m.sections for sub in s.subsections
        )
        summary = (
            f"Structure complete: {len(modules)} modules, {n_sec} sections, "
            f"{n_sub} subsections, {n_subsub} sub-subsections"
            f" — ready for human review."
        )
    else:
        lines = [f"{len(issues)} issue(s) found:"]
        for iss in issues:
            lines.append(f"  [{iss.level.upper()}] {iss.path} — {iss.reason}")
        summary = "\n".join(lines)
    return EvaluationResult(passed=passed, issues=issues, summary=summary)


# ── Email helpers ─────────────────────────────────────────────────────────────

def _format_email_body(modules: list[CTDModule], evaluation: EvaluationResult) -> str:
    sep = "=" * 60
    status = "PASSED" if evaluation.passed else "ISSUES FOUND"
    lines = [
        "ICH CTD Canonical Folder Structure — Approval Required",
        sep, "",
        f"Evaluation : {status}",
        evaluation.summary, "",
        "── Proposed folder structure ──────────────────────────────────────",
    ]
    flagged = {i.path: i.reason for i in evaluation.issues}
    for mod in modules:
        ann = f"  ◄ {flagged[mod.key]}" if mod.key in flagged else ""
        lines.append(f"\n{mod.key}/  {mod.label}{ann}")
        for sec in mod.sections:
            sec_path = f"{mod.key}/{sec.key}"
            ann = f"  ◄ {flagged[sec_path]}" if sec_path in flagged else ""
            lines.append(f"  {sec.key}/  {sec.label}{ann}")
            for sub in sec.subsections:
                sub_path = f"{mod.key}/{sec.key}/{sub.key}"
                ann = f"  ◄ {flagged[sub_path]}" if sub_path in flagged else ""
                lines.append(f"    {sub.key}/  {sub.label}{ann}")
                for subsub in sub.sub_subsections:
                    ss_path = f"{mod.key}/{sec.key}/{sub.key}/{subsub.key}"
                    ann = f"  ◄ {flagged[ss_path]}" if ss_path in flagged else ""
                    lines.append(f"      {subsub.key}/  {subsub.label}{ann}")
    lines += [
        "", sep,
        "Reply APPROVE or REJECT to this email, or confirm in the terminal.",
    ]
    return "\n".join(lines)


def _send_approval_email(to_address: str, subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_address
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(host, port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(user, to_address, msg.as_string())


# ── Graph nodes ───────────────────────────────────────────────────────────────

def load_canonical(state: EnrichState) -> dict:
    """Build the full module tree from the hardcoded canonical template."""
    modules = _build_canonical_modules()
    return {"modules": [m.model_dump() for m in modules]}


def enrich_labels(state: EnrichState) -> dict:
    """
    Optionally query the ICH index to refine human-readable labels.
    Keys are NEVER changed — only labels may be updated.
    If the index is unreachable the canonical labels are kept as-is.
    """
    url = state.get("ich_index_url", "")
    if not url:
        return {}

    modules = [CTDModule(**m) for m in state["modules"]]
    enriched: list[dict] = []

    for mod in modules:
        try:
            ans = _post_query(url, _LABEL_QUERY.format(level="module", key=mod.key))
            data = _extract_json_object(ans)
            if data and data.get("label"):
                mod = mod.model_copy(update={"label": data["label"]})
        except Exception:
            pass

        enriched_sections: list[CTDSection] = []
        for sec in mod.sections:
            try:
                ans = _post_query(url, _LABEL_QUERY.format(level="section", key=sec.key))
                data = _extract_json_object(ans)
                if data and data.get("label"):
                    sec = sec.model_copy(update={"label": data["label"]})
            except Exception:
                pass
            enriched_sections.append(sec)

        mod = mod.model_copy(update={"sections": enriched_sections})
        enriched.append(mod.model_dump())

    return {"modules": enriched}


def evaluate_completeness(state: EnrichState) -> dict:
    modules = [CTDModule(**m) for m in state["modules"]]
    evaluation = _evaluate(modules)
    return {"evaluation": evaluation.model_dump()}


def notify_human(state: EnrichState) -> dict:
    reviewer = state.get("reviewer_email") or os.environ.get("CTD_REVIEWER_EMAIL")
    if not reviewer:
        print("[ctd_structure] No reviewer email — skipping notification.")
        return {"human_notified": False}

    modules = [CTDModule(**m) for m in state["modules"]]
    evaluation = (
        EvaluationResult(**state["evaluation"])
        if state.get("evaluation")
        else EvaluationResult(passed=False, summary="Evaluation not available.")
    )
    subject = "[CTD Approval Required] ICH M4(R4) Canonical Structure"
    body = _format_email_body(modules, evaluation)
    try:
        _send_approval_email(reviewer, subject, body)
        print(f"[ctd_structure] Approval email sent → {reviewer}")
        return {"human_notified": True}
    except KeyError as exc:
        print(f"[ctd_structure] Email skipped — missing env var: {exc}")
    except Exception as exc:
        print(f"[ctd_structure] Email failed: {exc}")
    return {"human_notified": False}


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
    ctx = state.get("program_context") or {}
    study_map: dict[str, list[dict]] = ctx.get("studies", {})
    if not study_map:
        return {}

    modules = [CTDModule(**m) for m in state["modules"]]
    updated = False

    def _inject(sub: CTDSubsection) -> CTDSubsection:
        nonlocal updated
        new_children = [_inject(c) for c in sub.sub_subsections]
        for s in study_map.get(sub.key, []):
            if not s.get("key"):
                continue
            new_children.append(CTDSubsection(
                key=s["key"],
                label=s.get("label", s["key"]),
                sub_subsections=[],
            ))
            updated = True
        if new_children != list(sub.sub_subsections):
            return sub.model_copy(update={"sub_subsections": new_children})
        return sub

    result_modules: list[CTDModule] = []
    for mod in modules:
        if mod.key != "module5":
            result_modules.append(mod)
            continue
        new_sections = [
            sec.model_copy(update={"subsections": [_inject(s) for s in sec.subsections]})
            for sec in mod.sections
        ]
        result_modules.append(mod.model_copy(update={"sections": new_sections}))

    if updated:
        return {"modules": [m.model_dump() for m in result_modules]}
    return {}


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
    evaluation = EvaluationResult(**state["evaluation"])
    canonical_modules = _build_canonical_modules()
    canonical_by_key = {m.key: m for m in canonical_modules}

    flagged_mods: set[str] = set()
    for issue in evaluation.issues:
        flagged_mods.add(issue.path.split("/")[0])

    repaired: list[dict] = []
    for mod_data in state["modules"]:
        mod_key = mod_data["key"]
        if mod_key in flagged_mods and mod_key in canonical_by_key:
            print(f"[ctd_structure] repair: rebuilt canonical {mod_key}")
            repaired.append(canonical_by_key[mod_key].model_dump())
        else:
            repaired.append(mod_data)

    present = {m["key"] for m in repaired}
    for mod in canonical_modules:
        if mod.key not in present:
            print(f"[ctd_structure] repair: added missing {mod.key}")
            repaired.append(mod.model_dump())

    return {
        "modules": repaired,
        "repair_retries": state.get("repair_retries", 0) + 1,
    }


# ── Routing ────────────────────────────────────────────────────────────────────

def after_evaluate(state: EnrichState) -> str:
    """Route to repair loop or human notification after evaluation.

    Structural issues (missing modules, canonical paths absent) trigger
    ``repair_canonical`` — but only up to ``_MAX_REPAIR_RETRIES`` times to
    prevent infinite loops.  Duplicate/prefix issues are advisory and go
    directly to the reviewer.
    """
    evaluation = EvaluationResult(**state["evaluation"])
    structural = [
        i for i in evaluation.issues
        if i.level == "module"
        or i.reason.startswith("canonical ICH")
    ]
    if structural and state.get("repair_retries", 0) < _MAX_REPAIR_RETRIES:
        return "repair_canonical"
    return "notify_human"


# ── Graph assembly ────────────────────────────────────────────────────────────

def _build_graph():
    g = StateGraph(EnrichState)
    g.add_node("load_canonical",         load_canonical)
    g.add_node("expand_program_studies", expand_program_studies)
    g.add_node("enrich_labels",          enrich_labels)
    g.add_node("evaluate_completeness",  evaluate_completeness)
    g.add_node("repair_canonical",       repair_canonical)
    g.add_node("notify_human",           notify_human)

    g.set_entry_point("load_canonical")
    g.add_edge("load_canonical",          "expand_program_studies")
    g.add_edge("expand_program_studies",  "enrich_labels")
    g.add_edge("enrich_labels",           "evaluate_completeness")
    g.add_conditional_edges("evaluate_completeness", after_evaluate)
    g.add_edge("repair_canonical",        "evaluate_completeness")
    g.add_edge("notify_human",            END)
    return g.compile()


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
    print(f"[ctd_structure] refine_from_feedback: '{feedback[:120]}'")
    return extract_from_ich_index(ich_index_url, reviewer_email=None)


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
    result = _graph.invoke({
        "ich_index_url": ich_index_url or "",
        "reviewer_email": reviewer_email,
        "program_context": program_context,
        "modules": [],
        "evaluation": None,
        "repair_retries": 0,
        "human_notified": False,
    })

    modules = [CTDModule(**m) for m in result["modules"]]
    evaluation = (
        EvaluationResult(**result["evaluation"])
        if result.get("evaluation")
        else EvaluationResult(passed=False, summary="Evaluation did not run.")
    )
    return CTDStructureOutput(modules=modules, evaluation=evaluation), evaluation
