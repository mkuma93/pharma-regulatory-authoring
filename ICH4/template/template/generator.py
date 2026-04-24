"""
template/generator.py  (ICH4/template service)

LLM-based per-program CTD section content template generator.

Strategy
────────
One ChatOpenAI call per CTD module (5 calls total for a full ICH M4 CTD).
Each call asks the LLM to produce Markdown templates for every section in
that module, returned as a JSON array so they can be parsed and stored
individually.

Key difference from the root ICH4/index version:
  • The LlamaIndex query engine has been removed.
  • Instead, the caller (orchestrator) pre-fetches ICH context from the
    index service and passes it as ich_context: dict[str, str] | None
    (keyed by module_key, e.g. {"module2": "<retrieved text>"}).
  • This keeps the template service free of LlamaIndex dependencies.
"""
from __future__ import annotations

import json
import logging
import os

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from clinical.models import ClinicalDataManifest
from ctd_structure.structure import CTDStructureOutput

from .models import ProgramInfo, SectionTemplate

logger = logging.getLogger(__name__)

# ── Per-module expert personas ────────────────────────────────────────────────

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

_SYSTEM_RULES = """\
When generating templates:
- Follow ICH M4 guidelines (M4E for efficacy, M4Q for quality, M4S for safety).
- Write content that is specific to the drug, indication, and therapeutic area provided.
- Use {{placeholder}} syntax for information the document author must supply.
- Where ICH M4 requirements are provided in the prompt, use them verbatim — do not \
  invent requirements from training memory.
- Where requirements are marked [NOT RETRIEVED], preserve that flag so the author \
  knows to verify manually against the guideline.
- Keep language formal, precise, and regulatory-appropriate.

EVIDENCE-GROUNDING RULES FOR PLACEHOLDERS
─────────────────────────────────────────
Every placeholder you define in the Placeholders table MUST include a "Source" tag
that tells the future writer LLM exactly where the value must come from.
Use one of three source types:

  [SOURCE: clinical_data]
      The value is a numeric outcome, rate, or statistic that MUST come from
      registered clinical study data. The writer MUST NOT invent this value.
      Example: {{recovery_rate_3mo}}, {{n_enrolled}}, {{ae_rate_pct}}

  [SOURCE: regulatory_context]
      The value is a mechanism description, study design rationale, regulatory
      process narrative, or pharmacological class description. The writer may
      compose this from drug-class knowledge and guideline context — but MUST NOT
      make specific numeric claims without a data source.
      Example: {{mechanism_of_action}}, {{dose_selection_rationale}}

  [SOURCE: author_supplied]
      Information only the sponsor / study team can provide (e.g. internal study IDs,
      submission reference numbers, proprietary formulation details).
      The writer MUST write [DATA PENDING — author to supply] for these.
      Example: {{study_id_ba}}, {{regulatory_reference_submission}}

In the Placeholders table, append the appropriate [SOURCE: ...] tag to every row.
- Return ONLY valid JSON — no prose, no markdown fences.\
"""

_SYSTEM_PROMPT = _MODULE_PERSONA_FALLBACK + "\n\n" + _SYSTEM_RULES


def _get_module_persona(module_key: str) -> str:
    persona = _MODULE_PERSONAS.get(module_key, _MODULE_PERSONA_FALLBACK)
    return persona + "\n\n" + _SYSTEM_RULES


# ── Few-shot examples ─────────────────────────────────────────────────────────
# Derived from publicly available ICH M4 guidance documents and EMA/FDA EPARs
# (ibuprofen 400 mg film-coated tablet — reference product, EU public domain).

_FEW_SHOT_EXAMPLES = '''
--- FEW-SHOT EXAMPLE 1 (Module 2 — Clinical Overview, public data: ibuprofen 400 mg) ---

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
This section provides a critical analysis and integrated summary of the clinical data package
supporting the use of ibuprofen 400 mg film-coated tablets for the treatment of mild-to-moderate
pain and associated inflammatory conditions. The overview synthesises findings across
biopharmaceutics, clinical pharmacology, efficacy, and safety, and concludes with a benefit-risk
assessment in the context of the target patient population and available therapeutic alternatives.

## ICH M4 Requirements
Per ICH M4E(R2) Section 2.5:
- Provide a concise, integrated critical analysis of clinical information — not a simple
  recitation of study results. Assessors expect the author's scientific judgement to be
  visible throughout.
- Section 2.5.1 must justify the development programme: unmet need, mechanism of action,
  selection of indication, dose, and patient population.
- Section 2.5.2 must cover biopharmaceutic classification (BCS), absolute/relative
  bioavailability, relevant food-effect and formulation-comparison studies.
- Section 2.5.3 must summarise PK (Cmax, AUC, t½, protein binding, metabolic pathways,
  excretion routes) and PD endpoints linked to clinical dose selection.
- Section 2.5.4 must present integrated efficacy: study designs, primary endpoints,
  responder definitions, dose-response, clinically relevant effect sizes, and
  consistency across subgroups.
- Section 2.5.5 must present integrated safety: adverse events by System Organ Class,
  serious adverse events, deaths, discontinuations due to AEs, and special population
  data (elderly, renal/hepatic impairment, paediatrics if applicable).
- Section 2.5.6 must provide a structured benefit-risk conclusion: characterise the
  benefit clearly, quantify the key risks, and conclude whether the benefit-risk balance
  is favourable for the proposed indication/population/dose.

## Content Template

### 2.5.1 Product Development Rationale
Ibuprofen is a propionic acid derivative non-steroidal anti-inflammatory drug (NSAID) belonging
to the pharmacological class of non-selective COX inhibitors. It exerts its therapeutic effect
primarily through reversible inhibition of both cyclooxygenase-1 (COX-1) and cyclooxygenase-2
(COX-2), reducing prostaglandin synthesis at the site of inflammation and within the central
nervous system pain pathways.

{{product_development_rationale}}

The 400 mg oral dose was selected based on {{dose_selection_rationale}} and has been validated
across {{number_of_pivotal_trials}} pivotal controlled trials enrolling {{total_trial_population}}
subjects. The Rx-to-OTC switch was supported by the established safety record over
{{years_of_use}} years of post-market use and a risk-benefit assessment documented in the
clinical overview of the {{regulatory_reference_submission}}.

### 2.5.2 Overview of Biopharmaceutics
Ibuprofen 400 mg is formulated as {{formulation_description}} and classified as a BCS Class
{{bcs_class}} compound. Oral bioavailability is approximately {{bioavailability_percent}}%
under fasted conditions. {{food_effect_summary}}

{{biopharmaceutics_overview}}

Key biopharmaceutic studies supporting this formulation are summarised below:

| Study ID | Design | Comparator | Key Finding |
|---|---|---|---|
| {{study_id_ba}} | {{ba_study_design}} | {{ba_comparator}} | {{ba_key_finding}} |
| {{study_id_food}} | Food-effect, cross-over | Fasted vs. fed | {{food_effect_detail}} |

### 2.5.3 Overview of Clinical Pharmacology
Following oral administration of a single 400 mg dose under fasted conditions:
- **Cmax**: {{cmax_value}} μg/mL (range: {{cmax_range}})
- **Tmax**: {{tmax_value}} h (range: {{tmax_range}})
- **AUC(0-∞)**: {{auc_value}} μg·h/mL
- **t½**: {{half_life}} h
- **Protein binding**: >{{protein_binding_percent}}%, predominantly albumin

{{clinical_pharmacology_overview}}

Metabolism is primarily hepatic via CYP2C9 to inactive hydroxylated and carboxylated
metabolites ({{primary_metabolites}}). Renal excretion accounts for approximately
{{renal_clearance_percent}}% of the administered dose within {{excretion_timeframe}} hours.
Dose adjustment for {{special_population_dose_note}} is recommended based on
{{special_population_pk_summary}}.

### 2.5.4 Overview of Efficacy
The efficacy of ibuprofen 400 mg was evaluated in {{number_of_efficacy_trials}} randomised,
controlled trials in patients with {{primary_indication_population}}. The primary endpoint
across all trials was {{primary_efficacy_endpoint}} at {{primary_timepoint}}.

{{efficacy_overview}}

**Table 2.5.4-1: Summary of Pivotal Efficacy Studies**

| Study ID | Design | N | Primary Endpoint | Result | p-value | 95% CI |
|---|---|---|---|---|---|---|
| {{study_1_id}} | {{study_1_design}} | {{study_1_n}} | {{study_1_endpoint}} | {{study_1_result}} | {{study_1_p}} | {{study_1_ci}} |
| {{study_2_id}} | {{study_2_design}} | {{study_2_n}} | {{study_2_endpoint}} | {{study_2_result}} | {{study_2_p}} | {{study_2_ci}} |

Dose-response was demonstrated across the 200 mg–600 mg range: {{dose_response_summary}}.
The proportion of patients achieving ≥50% reduction in pain intensity (responder rate) was
{{responder_rate_400mg}}% for ibuprofen 400 mg vs {{responder_rate_placebo}}% for placebo
(OR {{odds_ratio}}, 95% CI {{or_ci}}).

Subgroup analyses across pre-specified subgroups (age ≥65 years, sex, renal function
category) showed {{subgroup_consistency_summary}}.

### 2.5.5 Overview of Safety
The integrated safety database comprises {{total_safety_n}} subjects exposed to at least one
dose of ibuprofen across {{number_of_safety_trials}} trials. Median exposure was
{{median_exposure_duration}} days (range {{exposure_range}}).

{{safety_overview}}

**Table 2.5.5-1: Summary of Adverse Events (Safety Population)**

| Category | Ibuprofen 400 mg (N={{ibu_n}}) | Placebo (N={{pbo_n}}) |
|---|---|---|
| Any AE, n (%) | {{ibu_any_ae}} | {{pbo_any_ae}} |
| Any SAE, n (%) | {{ibu_sae}} | {{pbo_sae}} |
| Deaths, n (%) | {{ibu_deaths}} | {{pbo_deaths}} |
| Discontinuations due to AE, n (%) | {{ibu_disc_ae}} | {{pbo_disc_ae}} |

Most common adverse events (≥2% in the ibuprofen arm): {{most_common_aes}}.
GI events (nausea, dyspepsia, abdominal pain) were the most frequently reported class:
{{gi_ae_rate}}%.

Special population safety findings: {{special_population_safety_summary}}.

### 2.5.6 Benefits and Risks Conclusions
**Benefit characterisation**: Ibuprofen 400 mg produces a clinically meaningful reduction in
mild-to-moderate pain with an NNT of {{nnt_value}} (95% CI {{nnt_ci}}) for ≥50% pain relief
at {{primary_timepoint}}. The onset of action (median time to meaningful relief:
{{onset_median}} min) is clinically relevant for acute pain management.

**Risk characterisation**: The primary risks associated with ibuprofen 400 mg at the proposed
OTC dose and duration (≤{{max_otc_duration}} days) are {{primary_risk_summary}}. Absolute risk
increase for serious GI events vs. placebo over the trial duration was {{gi_ari}}% (NNH
{{gi_nnh}}).

{{benefit_risk_conclusions}}

**Conclusion**: The benefit-risk balance of ibuprofen 400 mg is considered {{br_conclusion}} for
the proposed indication of {{final_indication_wording}} in {{final_patient_population}}.
The risks are {{risk_mitigation_summary}}, aligning with the established OTC safety profile.

## Placeholders
| Placeholder | Expected Content & Guidance |
|---|---|
| `{{product_development_rationale}}` | ~350 words. Describe the unmet need at the time of initial development, the pharmacological rationale for COX inhibition in pain/inflammation, the nonclinical studies that supported translation to human dose (e.g., analgesia models, ED50), and the regulatory milestones (IND, NDA, OTC switch). Cite key study IDs where available. Begin: "The development of ibuprofen 400 mg was driven by the clinical need for..." |
| `{{dose_selection_rationale}}` | 1–2 sentences. State the dose-ranging study or PK/PD modelling basis for selecting 400 mg as the therapeutic dose. Reference study ID. |
| `{{number_of_pivotal_trials}}` | Integer (e.g., "6"). |
| `{{total_trial_population}}` | Integer. Total unique subjects across all pivotal efficacy trials. |
| `{{years_of_use}}` | Integer (e.g., "50"). |
| `{{regulatory_reference_submission}}` | Full submission reference (e.g., "NDA 18-463"). |
| `{{formulation_description}}` | Short description, e.g., "immediate-release film-coated tablet with hydroxypropyl methylcellulose (HPMC) coating". |
| `{{bcs_class}}` | Roman numeral class, e.g., "II". |
| `{{bioavailability_percent}}` | Number, e.g., "87". |
| `{{food_effect_summary}}` | 1–2 sentences. State whether food significantly affects AUC or Cmax (>20% change), and whether administration with food is required, optional, or not recommended. |
| `{{biopharmaceutics_overview}}` | ~250 words. Describe the in vitro dissolution profile, BCS classification evidence, comparability to reference product (if applicable), and any special biopharmaceutic considerations (e.g., enteric coating, modified release). |
| `{{study_id_ba}}`, `{{ba_study_design}}`, `{{ba_comparator}}`, `{{ba_key_finding}}` | One row per key BA/BE study. Keep finding to ≤15 words. |
| `{{study_id_food}}`, `{{food_effect_detail}}` | The food-effect study reference and its key PK finding (e.g., "Cmax delayed 1.5 h, AUC unaffected"). |
| `{{cmax_value}}`, `{{cmax_range}}` | Mean ± SD and observed range from the primary PK study. |
| `{{tmax_value}}`, `{{tmax_range}}` | Same format as Cmax. |
| `{{auc_value}}` | Mean AUC(0-∞) ± SD, e.g., "64.5 ± 11.2". |
| `{{half_life}}` | Mean terminal half-life, e.g., "2.0". |
| `{{protein_binding_percent}}` | Numeric, e.g., "99". |
| `{{clinical_pharmacology_overview}}` | ~300 words. Describe PK linearity across the dose range, clinically significant DDI findings (esp. CYP2C9 inhibitors, aspirin interaction), PK in elderly (≥65 years), hepatic impairment (Child–Pugh A/B), and renal impairment (eGFR categories). Highlight any clinically actionable findings. |
| `{{primary_metabolites}}` | List metabolite names, e.g., "hydroxy-ibuprofen and carboxy-ibuprofen". |
| `{{renal_clearance_percent}}` | Number, e.g., "90". |
| `{{excretion_timeframe}}` | Number of hours, e.g., "24". |
| `{{special_population_dose_note}}` | Short phrase, e.g., "severe renal impairment (eGFR <30 mL/min)". |
| `{{special_population_pk_summary}}` | 1 sentence describing the PK alteration and the dose recommendation. |
| `{{number_of_efficacy_trials}}` | Integer. |
| `{{primary_indication_population}}` | Description of enrolled population, e.g., "adult patients (≥18 years) with acute mild-to-moderate pain following dental extraction (ODT model)". |
| `{{primary_efficacy_endpoint}}` | Full endpoint name and instrument, e.g., "mean change from baseline in pain intensity on a 100 mm Visual Analogue Scale (VAS)". |
| `{{primary_timepoint}}` | e.g., "2 hours post-dose". |
| `{{efficacy_overview}}` | ~400 words. Provide a narrative synthesis of efficacy results: discuss the magnitude of the treatment effect vs. placebo (mean difference, 95% CI), time to onset, duration of pain relief (e.g., time to rescue medication), consistency across trial populations (dental, dysmenorrhea, headache models), and any evidence of dose–relationship within the 200–800 mg range. Conclude with a statement on clinical meaningfulness of the observed effect size relative to validated thresholds (e.g., MCID on VAS). |
| `{{study_1_id}}` through `{{study_2_ci}}` | Complete from the pivotal efficacy study tables. Add rows as needed. |
| `{{dose_response_summary}}` | 1–2 sentences describing the dose–response relationship (e.g., "Efficacy plateaued at 400 mg; 600 mg showed no statistically significant additional benefit on the primary endpoint"). |
| `{{responder_rate_400mg}}`, `{{responder_rate_placebo}}` | Percentages. |
| `{{odds_ratio}}`, `{{or_ci}}` | Numeric OR and 95% CI. |
| `{{subgroup_consistency_summary}}` | 1–2 sentences. State whether the primary endpoint result was consistent (no clinically meaningful heterogeneity) or whether any pre-specified subgroup showed a differential effect. |
| `{{total_safety_n}}` | Integer. |
| `{{number_of_safety_trials}}` | Integer. |
| `{{median_exposure_duration}}`, `{{exposure_range}}` | Numbers (days). |
| `{{safety_overview}}` | ~400 words. Describe the overall safety profile: most common AEs by System Organ Class (SOC) and preferred term, with incidence rates (ibuprofen vs. comparator), comparison of SAE rates, GI AE profile in detail (peptic ulcer, haemorrhage — incidence with 95% CI), cardiovascular signal assessment (any difference from placebo in MACE events), renal AEs (serum creatinine increases, acute kidney injury), hypersensitivity/skin reactions, and hepatic AEs. Mention any dose-dependent AE trends observed within the trial dose range. |
| `{{ibu_n}}`, `{{pbo_n}}` | Safety population sizes. |
| `{{ibu_any_ae}}`, `{{pbo_any_ae}}` | e.g., "245 (62.3%)". |
| `{{ibu_sae}}`, `{{pbo_sae}}` | e.g., "8 (2.0%)". |
| `{{ibu_deaths}}`, `{{pbo_deaths}}` | e.g., "0 (0.0%)". |
| `{{ibu_disc_ae}}`, `{{pbo_disc_ae}}` | e.g., "18 (4.6%)". |
| `{{most_common_aes}}` | Comma-separated list of AE preferred terms with incidence, e.g., "nausea (8.4%), headache (6.2%), dyspepsia (5.7%)". |
| `{{gi_ae_rate}}` | Number. |
| `{{special_population_safety_summary}}` | 1–3 sentences. Key safety findings in elderly (≥65 years), renally/hepatically impaired patients, or paediatrics if data are available. |
| `{{nnt_value}}`, `{{nnt_ci}}` | Number needed to treat for ≥50% pain relief, with CI. |
| `{{onset_median}}` | Median time to meaningful relief in minutes. |
| `{{max_otc_duration}}` | Maximum recommended OTC treatment duration in days, e.g., "3". |
| `{{primary_risk_summary}}` | Short phrase listing the 2–3 primary risks with their characterised severity, e.g., "GI irritation and peptic ulceration (common, dose- and duration-dependent), renal insufficiency in pre-disposed patients (uncommon), hypersensitivity reactions (rare)". |
| `{{gi_ari}}`, `{{gi_nnh}}` | Absolute risk increase (%) and number needed to harm for serious GI events. |
| `{{benefit_risk_conclusions}}` | ~300 words. Structured benefit-risk narrative: (1) Restate the magnitude of benefit (effect size, NNT) in plain clinical language; (2) Describe the risk profile quantitatively; (3) Contextualise against existing therapeutic alternatives (paracetamol, naproxen); (4) Identify the benefit-risk-optimised population and conditions of use; (5) State the overall assessment. End with one declarative sentence beginning: "On balance, the clinical data support that..." |
| `{{br_conclusion}}` | Regulatory conclusion word: "favourable" or "unfavourable". |
| `{{final_indication_wording}}` | Verbatim proposed indication text as it will appear on the label. |
| `{{final_patient_population}}` | Target patient demographic, e.g., "adults (≥18 years) without contraindications to NSAID therapy". |
| `{{risk_mitigation_summary}}` | 1 sentence describing the risk minimisation measures (label warnings, contraindications, maximum dose/duration restrictions). |

--- END FEW-SHOT EXAMPLE 1 ---
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
{prior_evidence_block}
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
  Each subsection MUST include:
    (a) 1–3 sentences of regulatory context or framing prose before the placeholder —
        explaining what this subsection is for and what the author should keep in mind.
    (b) A {{{{placeholder}}}} for the author-supplied narrative content, plus any
        supporting data tables (using | col | col | format) or lists as appropriate.
  Use {{{{placeholder}}}} syntax where author input is needed.
  If the section has subsections, add a `### <subsection_label>` heading for each one.

  ## Placeholders
  A table listing EVERY placeholder used in the Content Template above.
  The Description column must be detailed: include expected word count or data type,
  the specific regulatory/scientific content expected, and (for narrative placeholders
  ≥100 words) an example sentence starter beginning with "Begin:".

  | Placeholder | Expected Content & Guidance |
  |---|---|
  | `{{{{drug_name}}}}` | The INN or compound name. |
  | ... (all other placeholders — one row each, with rich guidance per the example) |

Return a JSON array — nothing else.
"""

# ── ICH requirements block ────────────────────────────────────────────────────

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


def _build_ich_requirements_block(ich_context_text: str | None) -> str:
    """Format a pre-fetched ICH context string into a structured prompt block.

    Args:
        ich_context_text: ICH guideline text retrieved by the orchestrator from
                          the index service for this module. None → fallback block.

    Returns:
        A formatted string to inject into the section-generation prompt.
    """
    if ich_context_text is None:
        return _ICH_REQUIREMENTS_FALLBACK

    text = ich_context_text.strip()
    if not text:
        return _ICH_REQUIREMENTS_FALLBACK

    return "\n" + _ICH_REQUIREMENTS_HEADER + text + "\n"


def _build_prior_evidence_block(prior_evidence: dict[str, str] | None) -> str:
    """Format prior-pass generated section content into a structured prompt block.

    Converts the orchestrator's {namespace: answer} evidence dict into a clearly
    delimited prompt block.  Returns an empty string when no evidence is supplied
    (e.g. for the first pass / Module 5).
    """
    if not prior_evidence:
        return ""

    _SEP = "\u2500" * 72
    lines = [
        "",
        _SEP,
        "PRIOR MODULE EVIDENCE (from already-generated CTD sections)",
        "Use the findings below when writing this module.  Rules:",
        "  1. DO NOT contradict these findings with different numbers or conclusions.",
        "  2. Cross-reference them explicitly (e.g. 'As reported in Module 5...').",
        "  3. Treat them as the primary source; ICH guidelines provide the structure.",
        _SEP,
    ]
    for ns, text in prior_evidence.items():
        # Derive a human-readable label from namespace suffix.
        # Namespace format: program_{ta}_{dis}_{drug}_{pass_id}
        # pass_id may be multi-word (e.g. module2_clinical_summary) — find the
        # first part that starts with "module" to avoid truncating at the last _.
        parts = ns.split("_")
        pass_id = next(
            ("_".join(parts[i:]) for i, p in enumerate(parts) if p.startswith("module")),
            parts[-1],
        )
        label = pass_id.replace("_", " ").title()
        lines.append(f"\n[Evidence source: {label}]")
        lines.append(text.strip())

    return "\n".join(lines) + "\n"


# ── Clinical data injection ───────────────────────────────────────────────────

def _clinical_context_block(section_key: str, manifest: ClinicalDataManifest) -> str:
    """Build a Markdown block listing clinical datasets relevant to *section_key*."""
    relevant: list[tuple] = []
    for source in manifest.sources:
        matching_cols = [
            cm for cm in source.column_mappings
            if any(
                section_key == sk
                or section_key.startswith(sk)
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
    ich_context: dict[str, str] | None = None,
    prior_evidence: dict[str, str] | None = None,
    api_key: str | None = None,
    model: str = "gpt-4o",
) -> list[SectionTemplate]:
    """Generate markdown content templates for all sections of a CTD program.

    Makes one LLM call per module (5 calls for a full ICH M4 CTD) and
    returns one :class:`SectionTemplate` per section.

    Args:
        program:           Drug program (therapeutic area, disease, drug name).
        ctd_output:        Assembled CTDStructureOutput from the extraction pipeline.
        clinical_manifest: Optional clinical data manifest. When provided, sections
                           that have registered clinical data will have a
                           "Clinical Data Available" block appended to their template.
        ich_context:       Optional dict keyed by module_key (e.g. "module2") containing
                           ICH guideline text pre-fetched by the orchestrator from the
                           index service. When provided, ICH requirements are grounded
                           in actual guideline PDFs rather than LLM training memory.
        prior_evidence:    Optional dict keyed by program namespace (e.g.
                           'program_neurology_bells_palsy_prednisolone_module5') →
                           RAG answer text from previously generated CTD sections.
                           Injected into the prompt so Module 2 summaries cite actual
                           CSR findings (ICH M4E(R2) evidence chain).
        api_key:           OpenAI API key (falls back to OPENAI_API_KEY env var).
        model:             OpenAI model to use (default gpt-4o).

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
            ich_context.get(module.key) if ich_context else None
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
            prior_evidence_block=_build_prior_evidence_block(prior_evidence),
        )

        messages = [
            SystemMessage(content=_get_module_persona(module.key)),
            HumanMessage(content=prompt),
        ]
        serialised_messages = [
            {"role": m.type, "content": m.content} for m in messages
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
                        prompt_messages=serialised_messages,
                    )
                )

        except Exception as exc:
            logger.error(
                "[template.generator] Failed to generate templates for %s: %s",
                module.key, exc, exc_info=True,
            )
            raise

    return all_templates
