"""
template/generator.py

LLM-based per-program CTD section content template generator.

Strategy
────────
One ChatOpenAI call per CTD module (5 calls total for a full ICH M4 CTD).
Each call asks the LLM to produce Markdown templates for every section in
that module, returned as a JSON array so they can be parsed and stored
individually.

The templates:
  • Explain the ICH M4 purpose of the section
  • List what ICH M4 requires the section to contain
  • Provide a structured content scaffold with {{placeholder}} syntax,
    personalised to the drug / indication / therapeutic area
  • Include a placeholder reference table at the bottom
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ctd_structure.structure import CTDStructureOutput

from .models import ProgramInfo, SectionTemplate

if TYPE_CHECKING:
    from clinical.models import ClinicalDataManifest
    from llama_index.core.query_engine import BaseQueryEngine
# ── Prompts ───────────────────────────────────────────────────────────────────

# ── Per-module expert personas ────────────────────────────────────────────────
# Each value is the "who you are" paragraph injected into the system message.
# The common rules block (_SYSTEM_RULES) is always appended after the persona.
# Keyed by module_key (matches CTDModule.key from _ICH_M4_CANONICAL).

_MODULE_PERSONAS: dict[str, str] = {
    "module1": (
        "You are a senior regulatory affairs director with 15+ years of experience "
        "submitting INDs, NDAs, BLAs, and MAAs globally. "
        "Module 1 is the administrative and regional layer — you think in terms of "
        "completeness checklists, cover letter strategy, and regional authority "
        "expectations (FDA, EMA, PMDA). "
        "You know exactly what a project manager and regulatory operations team needs "
        "to prepare a complete, error-free submission package."
    ),
    "module2": (
        "You are a cross-functional team of senior experts assembled for Module 2 "
        "summary writing. Each section has its own expert perspective:\n"
        "  2.3 Quality Overall Summary    — pharmaceutical scientist (ICH Q8/Q9/Q10, "
        "CMC, manufacturing process, specifications, impurity profiles)\n"
        "  2.4 Nonclinical Overview        — pharmacologist (MOA, primary/secondary PD, "
        "off-target coverage, species selection rationale)\n"
        "  2.5 Clinical Overview           — senior clinical scientist and medical officer "
        "(benefit-risk, unmet need, patient population, pivotal study strategy)\n"
        "  2.6 Nonclinical Summaries       — pharmacologist (2.6.2/2.6.3 PD/PK) and "
        "toxicologist (2.6.6/2.6.7 tox summaries, GLP compliance, NOAEL justification)\n"
        "  2.7 Clinical Summary            — clinical pharmacologist (2.7.2 PK/PD, DDI, "
        "special populations), biostatistician (2.7.3 efficacy endpoints, pre-specified "
        "analyses, ITT/PP populations), safety physician (2.7.4 SAE narratives, "
        "signal detection, exposure-response)\n"
        "Adopt the appropriate sub-expert voice for each section you generate."
    ),
    "module3": (
        "You are a pharmaceutical scientist and CMC (Chemistry, Manufacturing and Controls) "
        "expert with deep experience in drug substance characterisation, drug product "
        "formulation development, process validation, and analytical method development. "
        "You think in terms of ICH Q6A/Q6B specifications, ICH Q2(R1) analytical "
        "validation, ICH Q1 stability, and Ph. Eur./USP/JP compendial standards. "
        "A Module 3 reviewer will scrutinise every batch record, impurity limit "
        "justification, and container closure system — your templates must anticipate "
        "that level of technical rigour."
    ),
    "module4": (
        "You are a nonclinical pharmacology and toxicology expert with hands-on GLP study "
        "design experience across pharmacology, safety pharmacology, ADME, single-dose "
        "and repeat-dose toxicology, genotoxicity, carcinogenicity, and reproductive "
        "toxicology. "
        "You approach Module 4 as a toxicologist preparing the study reports that must "
        "satisfy ICH S1-S9 and ICH M3(R2) requirements. "
        "Your primary concerns are species selection justification, NOAEL derivation, "
        "safety margins relative to the proposed clinical dose, and GLP compliance statements."
    ),
    "module5": (
        "You are a clinical scientist and biostatistician with extensive Phase I-III "
        "trial experience. You approach Module 5 as the person who designed the SAP, "
        "reviewed the CSR, and will defend every efficacy and safety table to a "
        "regulatory reviewer. "
        "You think in terms of ICH E3 (CSR structure), ICH E9 (statistical principles), "
        "ICH E6 (GCP), ITT vs. PP analyses, pre-specified sensitivity analyses, and the "
        "CONSORT flow of subjects through the trial. "
        "For registry or observational datasets, you apply epidemiological rigour — "
        "confounding, selection bias, and generalisability to the target population."
    ),
}

_MODULE_PERSONA_FALLBACK = (
    "You are a senior pharmaceutical regulatory scientist with broad expertise across "
    "drug development and ICH CTD submissions."
)


def _get_module_persona(module_key: str) -> str:
    """Return the full system prompt string for *module_key*, with shared rules appended."""
    persona = _MODULE_PERSONAS.get(module_key, _MODULE_PERSONA_FALLBACK)
    return persona + "\n\n" + _SYSTEM_RULES


# Shared rules appended to every persona (defined before _get_module_persona uses it)
_SYSTEM_RULES = """\
When generating templates:
- Follow ICH M4 guidelines (M4E for efficacy, M4Q for quality, M4S for safety).
- Write content that is specific to the drug, indication, and therapeutic area provided.
- Use {placeholder} syntax for information the document author must supply.
- Where ICH M4 requirements are provided in the prompt, use them verbatim — do not \
  invent requirements from training memory.
- Where requirements are marked [NOT RETRIEVED], preserve that flag so the author \
  knows to verify manually against the guideline.
- Keep language formal, precise, and regulatory-appropriate.
- Return ONLY valid JSON — no prose, no markdown fences.\
"""

# Generic fallback used by code paths that have not yet adopted per-module personas
_SYSTEM_PROMPT = _MODULE_PERSONA_FALLBACK + "\n\n" + _SYSTEM_RULES

# ── Few-shot examples ─────────────────────────────────────────────────────────
# Derived from publicly available ICH M4 guidance documents and EMA/FDA EPARs
# (ibuprofen 400 mg film-coated tablet — reference product, EU public domain).
# Two examples spanning different modules are included so the model learns the
# expected depth for both scientific-quality (Module 3) and narrative-summary
# (Module 2) sections.

_FEW_SHOT_EXAMPLES = '''
--- FEW-SHOT EXAMPLE 1 (Module 2 — Clinical Overview, public data: ibuprofen) ---

INPUT:
{
  "section_key": "2.5_clinical_overview",
  "section_label": "Clinical Overview",
  "subsections": [
    {"key": "2.5.1_product_development_rationale", "label": "Product Development Rationale"},
    {"key": "2.5.2_overview_of_biopharmaceutics", "label": "Overview of Biopharmaceutics"},
    {"key": "2.5.3_overview_of_clinical_pharmacology", "label": "Overview of Clinical Pharmacology"},
    {"key": "2.5.4_overview_of_efficacy", "label": "Overview of Efficacy"},
    {"key": "2.5.5_overview_of_safety", "label": "Overview of Safety"},
    {"key": "2.5.6_benefits_and_risks_conclusions", "label": "Benefits and Risks Conclusions"}
  ]
}

PROGRAM CONTEXT: Drug = ibuprofen 400 mg · Indication = mild-to-moderate pain · TA = pain management

OUTPUT (content field):
# 2.5 — Clinical Overview  (ibuprofen 400 mg · mild-to-moderate pain · pain management)
> Section module2/2.5_clinical_overview

---

## Purpose
This section provides a critical analysis of the clinical data package for ibuprofen 400 mg
film-coated tablets in the treatment of mild-to-moderate pain and associated inflammatory
conditions. It integrates evidence from pharmacology, biopharmaceutics, efficacy, and safety
studies to support the benefit-risk conclusion presented in Module 5.

## ICH M4 Requirements
Per ICH M4E(R2) Section 2.5:
- Provide a brief integrated summary of the clinical information for the product.
- Discuss the development programme: rationale, clinical pharmacology, efficacy, and safety.
- Address the benefit-risk balance and its implications for the proposed indication.
- Refer to individual study summaries located in Module 2.7 and full reports in Module 5.
- The overview should be 30 pages or fewer; it is not a simple summary of the data.

## Content Template

### 2.5.1 Product Development Rationale
Ibuprofen is a propionic acid-class NSAID first described by Adams et al. (1961) and approved
by the FDA in 1974. The development of {{formulation_description}} was undertaken to
{{development_rationale}} for patients with {{target_population}}.

The proposed indication is **{{indication}}** in adults and adolescents ≥ {{minimum_age}} years.

{{#regulatory_history}}
Prior regulatory approvals and any changes to the development programme are summarised in
Section 1.2 (Application Form). Key milestones: {{regulatory_milestones}}.
{{/regulatory_history}}

### 2.5.2 Overview of Biopharmaceutics
Ibuprofen is practically insoluble in water (solubility ≈ 0.021 mg/mL at 25 °C) and is a
BCS Class II compound (high permeability, low solubility). The absorption of ibuprofen is
dissolution-rate limited.

| Parameter | {{drug_name}} {{strength}} | Reference product |
|---|---|---|
| Formulation | {{formulation_type}} | {{reference_formulation}} |
| T_max (h) | {{tmax_value}} ± {{tmax_sd}} | {{ref_tmax}} ± {{ref_tmax_sd}} |
| C_max (ng/mL) | {{cmax_value}} ± {{cmax_sd}} | {{ref_cmax}} ± {{ref_cmax_sd}} |
| AUC_0–∞ (ng·h/mL) | {{auc_value}} ± {{auc_sd}} | {{ref_auc}} ± {{ref_auc_sd}} |

Bioequivalence to {{reference_product}} has been demonstrated in Study {{be_study_code}}
(see Module 5.3.1.2 for full report).

### 2.5.3 Overview of Clinical Pharmacology
Ibuprofen acts by non-selective inhibition of COX-1 and COX-2, reducing prostaglandin
synthesis. {{drug_name}} {{strength}} achieves therapeutic plasma concentrations within
{{time_to_effect}} of administration under {{fed_fasted}} conditions.

Key PK parameters in the target population:
- Half-life: approximately {{half_life}} hours
- Protein binding: {{protein_binding}}%
- Metabolised by: {{metabolic_pathway}} ({{cyp_enzyme}})
- Primary route of elimination: {{elimination_route}}

Special populations: {{special_pop_summary}}. Full details in Module 2.7.2.

### 2.5.4 Overview of Efficacy
Clinical efficacy of {{drug_name}} {{strength}} in {{indication}} is supported by:

{{#pivotal_studies}}
**{{study_id}}** ({{study_design}}): {{n_subjects}} subjects with {{diagnosis}}.
Primary endpoint ({{primary_endpoint}}): {{primary_result}} ({{p_value}}).
{{/pivotal_studies}}

The clinical relevance threshold was defined as {{clinical_relevance_threshold}}.
Response rates across pivotal trials are presented in Module 2.7.3.

### 2.5.5 Overview of Safety
The safety profile of ibuprofen 400 mg is well characterised from post-marketing experience
exceeding {{post_marketing_years}} years and from {{n_clinical_trial_subjects}} subjects
enrolled in clinical studies.

Most common adverse events (≥ 1%):
{{#adverse_events}}
- {{soc}}: {{preferred_terms}} ({{incidence}}%)
{{/adverse_events}}

Serious adverse events and risk minimisation measures are detailed in Module 2.7.4 and the
Risk Management Plan (Module 1.8.2).

### 2.5.6 Benefits and Risks Conclusions
The benefit-risk balance for {{drug_name}} {{strength}} in the proposed indication
**{{indication}}** is considered {{benefit_risk_conclusion}}.

{{benefit_summary}}

{{risk_summary}}

Proposed labelling reflects these findings (see Module 1.3 — Product Information).

---

## Placeholders
| Placeholder | Description |
|---|---|
| `{{drug_name}}` | Drug or compound name |
| `{{strength}}` | Formulation strength (e.g. 400 mg) |
| `{{formulation_description}}` | Brief formulation description (e.g. 400 mg film-coated tablet) |
| `{{formulation_type}}` | Dosage form (e.g. film-coated tablet, oral suspension) |
| `{{development_rationale}}` | Clinical/commercial rationale for developing this product |
| `{{target_population}}` | Target patient population |
| `{{indication}}` | Approved or proposed indication |
| `{{minimum_age}}` | Minimum age for proposed indication |
| `{{regulatory_history}}` | Prior approvals / regulatory milestones block (optional) |
| `{{regulatory_milestones}}` | Key dates and decisions |
| `{{reference_product}}` | Comparator / reference product name |
| `{{be_study_code}}` | Bioequivalence study identifier |
| `{{tmax_value}}`, `{{tmax_sd}}` | Test product T_max mean ± SD |
| `{{ref_tmax}}`, `{{ref_tmax_sd}}` | Reference T_max mean ± SD |
| `{{cmax_value}}`, `{{cmax_sd}}` | Test C_max mean ± SD |
| `{{ref_cmax}}`, `{{ref_cmax_sd}}` | Reference C_max mean ± SD |
| `{{auc_value}}`, `{{auc_sd}}` | Test AUC mean ± SD |
| `{{ref_auc}}`, `{{ref_auc_sd}}` | Reference AUC mean ± SD |
| `{{time_to_effect}}` | Time to peak analgesic effect (e.g. 45–60 minutes) |
| `{{fed_fasted}}` | Fed or fasted conditions for PK |
| `{{half_life}}` | Elimination half-life |
| `{{protein_binding}}` | % plasma protein binding |
| `{{metabolic_pathway}}` | Primary metabolic pathway (e.g. hepatic glucuronidation) |
| `{{cyp_enzyme}}` | Key CYP enzyme (e.g. CYP2C9) |
| `{{elimination_route}}` | Route of excretion (e.g. renal as metabolites) |
| `{{special_pop_summary}}` | One-line summary of special population PK findings |
| `{{study_id}}` | Pivotal study identifier |
| `{{study_design}}` | Study design (e.g. randomised double-blind placebo-controlled) |
| `{{n_subjects}}` | Number of subjects enrolled |
| `{{diagnosis}}` | Subject diagnosis in that study |
| `{{primary_endpoint}}` | Primary efficacy endpoint |
| `{{primary_result}}` | Numerical result for primary endpoint |
| `{{p_value}}` | p-value for primary endpoint |
| `{{clinical_relevance_threshold}}` | Minimum clinically important difference |
| `{{post_marketing_years}}` | Years of post-marketing experience |
| `{{n_clinical_trial_subjects}}` | Total clinical trial subjects exposed |
| `{{adverse_events}}` | Block repeated per SOC |
| `{{soc}}` | System organ class |
| `{{preferred_terms}}` | MedDRA preferred terms in that SOC |
| `{{incidence}}` | Incidence percentage |
| `{{benefit_risk_conclusion}}` | Overall B/R conclusion (e.g. "favourable") |
| `{{benefit_summary}}` | 2–3 sentence benefit narrative |
| `{{risk_summary}}` | 2–3 sentence risk narrative |


--- FEW-SHOT EXAMPLE 2 (Module 3 — Drug Product Description, public data: ibuprofen) ---

INPUT:
{
  "section_key": "3.2.p.1_description_and_composition",
  "section_label": "Description and Composition of the Drug Product",
  "subsections": []
}

PROGRAM CONTEXT: Drug = ibuprofen 400 mg · Indication = mild-to-moderate pain · TA = pain management

OUTPUT (content field):
# 3.2.P.1 — Description and Composition of the Drug Product  (ibuprofen 400 mg · mild-to-moderate pain · pain management)
> Section module3/3.2.p.1_description_and_composition

---

## Purpose
Section 3.2.P.1 provides a complete description of the finished drug product and its
quantitative composition, enabling regulatory agencies to confirm that the formulation
is appropriately characterised in accordance with ICH Q8(R2) and the Ph. Eur. / USP
monograph for ibuprofen tablets.

## ICH M4 Requirements
Per ICH M4Q(R1) Section 3.2.P.1:
- Describe the dosage form and its composition (all components, including overages).
- State the function of each component (active, diluent, binder, disintegrant, lubricant,
  film coat, etc.).
- Reference compendial standards where applicable (Ph. Eur., USP/NF, JP, in-house).
- Report quantities per unit dose and, where relevant, per batch.
- Include a description of the container closure system (see also 3.2.P.7).

## Content Template

**Dosage Form:** {{dosage_form}} (e.g. film-coated tablet)
**Route of Administration:** {{route_of_administration}} (e.g. oral)
**Strength:** {{strength}} (e.g. 400 mg ibuprofen per tablet)

### Composition Table

| Component | Function | Grade / Standard | Quantity per unit (mg) | Quantity per batch (kg) |
|---|---|---|---|---|
| Ibuprofen | Active pharmaceutical ingredient | Ph. Eur. / USP | {{api_quantity_mg}} | {{api_quantity_kg}} |
| {{excipient_1_name}} | {{excipient_1_function}} | {{excipient_1_grade}} | {{excipient_1_qty_mg}} | {{excipient_1_qty_kg}} |
| {{excipient_2_name}} | {{excipient_2_function}} | {{excipient_2_grade}} | {{excipient_2_qty_mg}} | {{excipient_2_qty_kg}} |
| {{excipient_3_name}} | {{excipient_3_function}} | {{excipient_3_grade}} | {{excipient_3_qty_mg}} | {{excipient_3_qty_kg}} |
| {{film_coat_name}} | Film coat (colour, moisture barrier) | {{film_coat_grade}} | {{film_coat_qty_mg}} | {{film_coat_qty_kg}} |
| **Total tablet weight** | | | **{{total_tablet_weight_mg}}** | **{{total_batch_weight_kg}}** |

_Note: No overage is included for the active substance. {{overage_note}}_

### Container Closure System
{{drug_name}} {{strength}} {{dosage_form}} is packaged in {{container_description}}
(e.g. PVC/aluminium blister packs of 12, 24, or 48 tablets).
Refer to Section 3.2.P.7 for full details of the container closure system.

### Description
Each {{dosage_form}} is {{physical_description}} (e.g. white to off-white, oval,
film-coated tablet, debossed with "{{deboss_marking}}" on one side).
Dimensions: {{tablet_length}} × {{tablet_width}} mm, thickness {{tablet_thickness}} mm.

---

## Placeholders
| Placeholder | Description |
|---|---|
| `{{dosage_form}}` | Type of dosage form (e.g. film-coated tablet) |
| `{{route_of_administration}}` | Route (e.g. oral) |
| `{{strength}}` | Labelled strength (e.g. 400 mg ibuprofen) |
| `{{api_quantity_mg}}` | API quantity per unit in mg |
| `{{api_quantity_kg}}` | API quantity per batch in kg |
| `{{excipient_N_name}}` | Name of excipient N |
| `{{excipient_N_function}}` | Role of excipient N (binder, diluent, etc.) |
| `{{excipient_N_grade}}` | Compendial or in-house grade |
| `{{excipient_N_qty_mg}}` | Per-unit quantity in mg |
| `{{excipient_N_qty_kg}}` | Per-batch quantity in kg |
| `{{film_coat_name}}` | Film coat system trade name (e.g. Opadry White) |
| `{{film_coat_grade}}` | Film coat grade/standard |
| `{{film_coat_qty_mg}}` | Film coat quantity per unit in mg |
| `{{total_tablet_weight_mg}}` | Total tablet weight in mg |
| `{{total_batch_weight_kg}}` | Total batch weight in kg |
| `{{overage_note}}` | Note on any overage (or confirm none) |
| `{{container_description}}` | Container closure description |
| `{{physical_description}}` | Tablet appearance description |
| `{{deboss_marking}}` | Deboss / imprint on tablet |
| `{{tablet_length}}`, `{{tablet_width}}`, `{{tablet_thickness}}` | Tablet dimensions in mm |

--- END FEW-SHOT EXAMPLES ---
'''

_SECTION_PROMPT_TMPL = """\
Generate Markdown content templates for every CTD section listed below.

Study the following examples (derived from publicly available ICH M4 guidance and
EMA/FDA reference product data for ibuprofen) to understand the expected depth,
structure, and placeholder style:

{few_shot_examples}

Now generate templates for the actual program below, following the same style:

Program context
───────────────
  Drug / compound    : {drug_name}
  Indication         : {disease_type}
  Therapeutic area   : {therapeutic_area}
  CTD module         : {module_key} — {module_label}

Sections
────────
{sections_json}
{ich_retrieved_requirements}
For EACH section return one JSON object with exactly these keys:
  "section_key"   — copy from input
  "section_label" — copy from input
  "content"       — the full Markdown template (string)

The Markdown template for every section MUST contain these five parts,
separated by `---`:

  # <section_code> — <section_title>  ({drug_name} · {disease_type} · {therapeutic_area})
  > Section <module_key>/<section_key>

  ---

  ## Purpose
  2–3 sentences explaining what this section covers for {drug_name} in {disease_type}.

  ## ICH M4 Requirements
  Bullet list of what ICH guidelines require this section to contain.

  ## Content Template
  Structured regulatory content scaffold personalised for {drug_name}.
  Use {{{{placeholder}}}} syntax where author input is needed.
  If the section has subsections, add a `### <subsection_label>` heading for each one.

  ## Placeholders
  | Placeholder | Description |
  |---|---|
  | `{{{{drug_name}}}}` | Drug or compound name |
  | ... (all placeholders used above) |

Return a JSON array — nothing else.
"""


# ── ICH index retrieval (one query per module) ────────────────────────────────

_ICH_MODULE_QUERY_TMPL = (
    "What are all mandatory contents, required documents, and regulatory "
    "requirements from any applicable ICH guideline (including ICH M4, M4E, "
    "E3, E9, E9-R1, and any other relevant guideline) for {module_key} "
    "({module_label})? "
    "Include requirements for every section and subsection within this module, "
    "covering statistical principles, CSR structure, estimands, and any "
    "guideline-specific obligations where applicable."
)

_ICH_REQUIREMENTS_HEADER = (
    "Retrieved ICH guideline requirements for this module\n"
    "(source: indexed ICH guideline PDFs — M4, M4E, E3, E9, E9-R1 — authoritative)\n"
    "\n"
    "STRICT RULES FOR THE LLM:\n"
    "  1. Use only requirements explicitly stated in the text below for "
         "## ICH M4 Requirements.\n"
    "  2. Do NOT add requirements from training memory.\n"
    "  3. If the retrieved text does not cover a section, write:\n"
    "       > [NOT RETRIEVED — author must verify against ICH M4]\n"
    "     instead of inventing requirements.\n"
    "\u2500" * 72 + "\n"
)

_ICH_REQUIREMENTS_FALLBACK = (
    "\n"
    + "\u2500" * 72 + "\n"
    "WARNING: ICH guideline index was NOT available for this generation.\n"
    "\n"
    "STRICT RULES FOR THE LLM:\n"
    "  1. You MUST prefix every bullet in ## ICH M4 Requirements with:\n"
    "       > [NOT RETRIEVED — author must verify against ICH M4]\n"
    "  2. Do NOT present any requirement as authoritative — all are unverified\n"
    "     recalls from training data and may be incomplete or out of date.\n"
    "  3. The author is responsible for cross-checking against the current\n"
    "     ICH M4 guideline text before submission.\n"
    + "\u2500" * 72 + "\n"
)


def _build_ich_requirements_block(
    query_engine: object,
    module_key: str,
    module_label: str,
) -> str:
    """Run one LlamaIndex query for the entire module and return a formatted prompt block.

    One query per module keeps retrieval cost flat (5 calls for a full CTD) while
    providing broader, cross-section context so the LLM sees how requirements
    interrelate within the module.

    Returns a fallback warning block when ``query_engine`` is ``None`` or the
    query fails, so the LLM always marks unverified requirements visibly.
    """
    if query_engine is None:
        return _ICH_REQUIREMENTS_FALLBACK

    try:
        query = _ICH_MODULE_QUERY_TMPL.format(
            module_key=module_key, module_label=module_label
        )
        response = query_engine.query(query)  # type: ignore[attr-defined]
        text = str(response).strip()
    except Exception as exc:
        print(f"[template.generator] Warning: index query failed for {module_key}: {exc}")
        return _ICH_REQUIREMENTS_FALLBACK

    if not text:
        return _ICH_REQUIREMENTS_FALLBACK

    return "\n" + _ICH_REQUIREMENTS_HEADER + text + "\n"


# ── Clinical data injection ───────────────────────────────────────────────────

def _clinical_context_block(section_key: str, manifest: ClinicalDataManifest) -> str:
    """Build a Markdown block listing clinical datasets relevant to *section_key*.

    A column is considered relevant when any of its ``ctd_section_keys`` is a
    prefix of (or exactly matches) the template's section_key.  For example,
    column key ``"5.3.5.1"`` matches section_key
    ``"5.3.5.1_controlled_clinical_study_reports"``.

    Returns an empty string when no relevant data exists.
    """
    relevant: list[tuple] = []
    for source in manifest.sources:
        matching_cols = [
            cm for cm in source.column_mappings
            if any(
                # exact match
                section_key == sk
                # template section is a child of the column's key: "5.3_..." starts with "5.3"
                or section_key.startswith(sk)
                # column key is a child of the template section: "5.3.5.1" starts with "5.3"
                or sk.startswith(section_key.split("_")[0])
                for sk in cm.ctd_section_keys
            )
        ]
        if matching_cols:
            relevant.append((source, matching_cols))

    if not relevant:
        return ""

    lines = [
        "",
        "---",
        "",
        "## Clinical Data Available",
        "The following registered datasets provide source data for this section:",
        "",
    ]
    for source, cols in relevant:
        lines.append(f"### {source.filename} ({source.study_type})")
        lines.append(f"**GCS:** `{source.gcs_path}`")
        lines.append("")
        lines.append("| Column | Role | Placeholder |")
        lines.append("|---|---|---|")
        for cm in cols:
            lines.append(
                f"| {cm.column_name} | {cm.role} | `{{{{{cm.placeholder_key}}}}}` |"
            )
        lines.append("")

    return "\n".join(lines)


# ── Core generator ────────────────────────────────────────────────────────────

def generate_program_templates(
    program: ProgramInfo,
    ctd_output: CTDStructureOutput,
    clinical_manifest: ClinicalDataManifest | None = None,
    query_engine: BaseQueryEngine | None = None,
    api_key: str | None = None,
    model: str = "gpt-4o",
) -> list[SectionTemplate]:
    """Generate markdown content templates for all sections of a CTD program.

    Makes one LLM call per module (5 calls for a full ICH M4 CTD) and
    returns one :class:`SectionTemplate` per section.

    Args:
        program:           Drug program (therapeutic area, disease, drug name).
        ctd_output:        Assembled CTDStructureOutput from the extraction pipeline.
        clinical_manifest: Optional clinical data manifest.  When provided, sections
                           that have registered clinical data will have a
                           "Clinical Data Available" block appended to their template,
                           listing the relevant columns and their ``{{placeholder}}`` keys.
                           The clinical block is **reference only** \u2014 placeholder keys
                           pointing to source data, never generated content.
        query_engine:      Optional LlamaIndex query engine over the ICH guideline index.
                           When provided, each section's ICH M4 requirements are retrieved
                           from the index and injected verbatim into the prompt, grounding
                           the generated template in the actual guideline PDFs rather than
                           the LLM's training memory.
        api_key:           OpenAI API key (falls back to ``OPENAI_API_KEY`` env var).
        model:             OpenAI model to use (default ``gpt-4o``).

    Returns:
        List of :class:`SectionTemplate` objects, one per CTD section.
    """
    effective_key = api_key or os.environ.get("OPENAI_API_KEY")
    llm = ChatOpenAI(model=model, temperature=0.2, api_key=effective_key)

    all_templates: list[SectionTemplate] = []

    for module in ctd_output.modules:
        if not module.sections:
            continue

        sections_input = [
            {
                "section_key":   sec.key,
                "section_label": sec.label,
                "subsections": [
                    {"key": sub.key, "label": sub.label}
                    for sub in sec.subsections
                ],
            }
            for sec in module.sections
        ]

        ich_requirements_block = _build_ich_requirements_block(
            query_engine, module.key, module.label
        )

        prompt = _SECTION_PROMPT_TMPL.format(
            few_shot_examples=_FEW_SHOT_EXAMPLES,
            drug_name=program.drug_name,
            disease_type=program.disease_type,
            therapeutic_area=program.therapeutic_area,
            module_key=module.key,
            module_label=module.label,
            sections_json=json.dumps(sections_input, indent=2),
            ich_retrieved_requirements=ich_requirements_block,
        )

        messages = [
            SystemMessage(content=_get_module_persona(module.key)),
            HumanMessage(content=prompt),
        ]

        try:
            response = llm.invoke(messages)
            raw = response.content.strip()

            # Strip markdown code fences if the LLM wraps the JSON
            if raw.startswith("```"):
                parts = raw.split("```", 2)
                raw = parts[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.rsplit("```", 1)[0].strip()

            section_results = json.loads(raw)

            for item in section_results:
                content = item["content"]
                if clinical_manifest is not None:
                    extra = _clinical_context_block(item["section_key"], clinical_manifest)
                    if extra:
                        content = content + "\n" + extra
                all_templates.append(
                    SectionTemplate(
                        module_key=module.key,
                        module_label=module.label,
                        section_key=item["section_key"],
                        section_label=item["section_label"],
                        content=content,
                    )
                )

        except Exception as exc:
            print(
                f"[template.generator] Warning: failed to generate templates "
                f"for {module.key}: {exc}"
            )

    return all_templates
