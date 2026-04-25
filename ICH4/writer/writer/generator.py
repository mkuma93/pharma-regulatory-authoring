"""LLM-based content generator.

Reads approved template .md files from GCS, extracts {{placeholder}} keys,
loads relevant clinical data, and calls the LLM to fill every placeholder
with real regulatory prose.

One LLM call per section — keeps token windows manageable.
"""
from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .clinical_reader import build_clinical_context
from .models import ProgramInfo, SectionDocument
from .placeholder import extract_placeholders, fill_placeholders

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a senior pharmaceutical regulatory scientist writing CTD submission content
for a regulatory dossier (IND / NDA / MAA). Your output will be reviewed by health
authorities. Accuracy and evidence-grounding are mandatory.

TASK
────
Fill every {{placeholder}} in the template provided. Generate complete, regulatory-quality
prose for every placeholder. Leaving placeholders empty or unfilled is NOT acceptable.

PLACEHOLDER TYPES AND RULES
────────────────────────────
There are TWO types of placeholders. Apply the correct rule for each:

TYPE A — NARRATIVE / OVERVIEW / REPORT PLACEHOLDERS
   These describe study designs, regulatory rationale, mechanism of action, background
   context, overview summaries, PK/PD principles, safety profiles, benefit-risk
   assessments, or section-level reports (e.g., biopharmaceutic_reports, safety_overview,
   pk_studies_reports, efficacy_overview, benefit_risk_conclusions, etc.).
   ► RULE A: ALWAYS generate appropriate, publication-quality regulatory prose for these
     placeholders, informed by the drug class, indication, ICH guidelines, and any
     clinical data provided. Do NOT write [DATA PENDING] for narrative placeholders.
   ► Base the prose on established scientific/regulatory knowledge of the drug and indication.
   ► You MUST produce substantive prose — 1 to 5 paragraphs as appropriate to the section.

TYPE B — SPECIFIC NUMERIC / STATISTICAL VALUE PLACEHOLDERS
   These require exact figures: patient counts (N), percentages, p-values, ORs, CIs,
   NNTs, hazard ratios, AE rates, or any other specific quantitative result that
   MUST come from an actual study.
   ► RULE B: Use ONLY values from the "CLINICAL DATA FOR PLACEHOLDERS" section below.
   ► If no clinical data supports a specific statistic → write:
       [DATA PENDING — {placeholder_key}: no source data supplied]
   ► NEVER invent, estimate, or extrapolate a number.

CROSS-REFERENCE REQUIREMENT (both types)
─────────────────────────────────────────
   For every efficacy or safety figure you include, append an inline source tag:
       (Source: {filename}, {column_name})
   where filename and column_name come from the supplied clinical data header.
   If you cannot tag a specific figure with a source, it must be [DATA PENDING].

FORMATTING
──────────
- Keep all section headings, table structures, and subsection labels exactly as-is.
- Only replace {{placeholder}} tokens — do NOT rewrite framing prose.
- Return ONLY the completed Markdown document. No commentary, no JSON wrapping.
"""

_USER_TMPL = """\
Program: {drug_name} | {disease_type} | {therapeutic_area}
Section: {section_key} — {section_label}

=== TEMPLATE TO FILL ===
{template_content}

=== RESOLVED CLINICAL STATISTICS (pre-verified; use verbatim in any relevant narrative placeholder) ===
{resolved_stats}

=== CLINICAL DATA FOR PLACEHOLDERS ===
{clinical_context}

─────────────────────────────────────────────────────────────────────────────────
REMINDER:
• TYPE A (narrative/overview/report placeholders): Write complete, publication-quality
  regulatory prose using the statistics from "CLINICAL DATA" above as evidence.
  The statistics are your source — do NOT write [DATA PENDING] for narrative placeholders.
• TYPE B (specific numeric values — percentages, p-values, patient counts, CIs):
  Use ONLY exact figures from the "CLINICAL DATA" section above, with source tag.
  If a specific statistic has no source data: [DATA PENDING — {{placeholder_key}}: no source data supplied]
─────────────────────────────────────────────────────────────────────────────────
"""


def _fmt_clinical_context(context: dict[str, str]) -> str:
    if not context:
        return (
            "NO CLINICAL DATA SUPPLIED.\n"
            "For every numeric placeholder, write:\n"
            "  [DATA PENDING — {placeholder_key}: no source data supplied]\n"
            "Do NOT invent any figures."
        )
    parts: list[str] = []
    for key, data in context.items():
        parts.append(
            f"--- Clinical data for {{{{  {key}  }}}} ---\n"
            f"{data}"
        )
    return "\n\n".join(parts)


_PATCH_SYSTEM_PROMPT = """\
You are a senior pharmaceutical regulatory scientist updating an existing CTD
submission section because the underlying clinical dataset has changed.

TASK
────
A subset of {{placeholder}} values in the document below have NEW data.
Revise ONLY the sentences and tables that reference the changed placeholders.
All other prose must remain VERBATIM — do not rewrite, expand, or improve
unaffected text.

STRICT RULES
────────────
1. Changed placeholders are listed under "CHANGED PLACEHOLDERS".
   Update every sentence / cell / figure that uses one of those keys using
   the new values in "CLINICAL DATA FOR PLACEHOLDERS".
2. For every revised figure append an inline source tag: (Source: {filename}, {column_name}).
3. If a changed placeholder has no supporting data in the clinical section:
     [DATA PENDING — {placeholder_key}: no source data supplied]
4. Do NOT alter any section headings, table structure, or unaffected prose.
5. Return ONLY the complete revised Markdown document. No commentary.
"""

_PATCH_USER_TMPL = """\
Program: {drug_name} | {disease_type} | {therapeutic_area}
Section: {section_key} — {section_label}

CHANGED PLACEHOLDERS (update only these)
─────────────────────────────────────────
{changed_keys_list}

=== EXISTING DOCUMENT (keep unchanged sections verbatim) ===
{prior_content}

=== CLINICAL DATA FOR PLACEHOLDERS ===
{clinical_context}

─────────────────────────────────────────────────────────────────────────────────
REMINDER: Revise ONLY sentences referencing the changed placeholders listed above.
Return the complete revised Markdown document only — no commentary.
─────────────────────────────────────────────────────────────────────────────────
"""


def write_section(
    program: ProgramInfo,
    section_key: str,
    section_label: str,
    module_key: str,
    module_label: str,
    template_content: str,
    bucket_name: str,
    llm: ChatOpenAI,
    resolved_values: dict[str, str] | None = None,
    prior_content: str | None = None,
    changed_keys: list[str] | None = None,
) -> SectionDocument:
    """Fill one template and return a SectionDocument with completed prose.

    When ``prior_content`` is provided the writer runs in *patch mode*: it
    receives the existing document as context and only revises sentences that
    reference placeholders listed in ``changed_keys``.
    """
    placeholders = extract_placeholders(template_content)
    logger.info("[writer] Section %s — %d placeholders: %s",
                section_key, len(placeholders), placeholders)

    clinical_ctx = build_clinical_context(
        bucket_name, program, placeholders,
        resolved_values=resolved_values,
        llm=llm,
    )

    if prior_content is not None:
        # ── Patch mode: update only changed placeholders ──────────────────────
        keys_list = "\n".join(f"  - {k}" for k in (changed_keys or placeholders))
        messages = [
            SystemMessage(content=_PATCH_SYSTEM_PROMPT),
            HumanMessage(content=_PATCH_USER_TMPL.format(
                drug_name=program.drug_name,
                disease_type=program.disease_type,
                therapeutic_area=program.therapeutic_area,
                section_key=section_key,
                section_label=section_label,
                changed_keys_list=keys_list or "(all placeholders)",
                prior_content=prior_content,
                clinical_context=_fmt_clinical_context(clinical_ctx),
            )),
        ]
    else:
        # ── Standard mode: fill all placeholders in template ──────────────────
        _rv = resolved_values or {}
        resolved_stats_str = (
            "\n".join(f"  {k}: {v}" for k, v in _rv.items())
            if _rv else "None provided."
        )
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_USER_TMPL.format(
                drug_name=program.drug_name,
                disease_type=program.disease_type,
                therapeutic_area=program.therapeutic_area,
                section_key=section_key,
                section_label=section_label,
                template_content=template_content,
                resolved_stats=resolved_stats_str,
                clinical_context=_fmt_clinical_context(clinical_ctx),
            )),
        ]

    response = llm.invoke(messages)
    filled_content = response.content.strip()

    # Safety net: fill any remaining un-touched placeholders
    remaining = extract_placeholders(filled_content)
    if remaining:
        logger.warning("[writer] %s: %d placeholders still present after LLM fill: %s",
                       section_key, len(remaining), remaining)
        filled_content = fill_placeholders(
            filled_content,
            {k: f"[DATA PENDING — {k}: no source data supplied]" for k in remaining},
        )

    # Serialise messages for audit archiving
    serialised_messages = [
        {"role": m.type, "content": m.content}
        for m in messages
    ]

    return SectionDocument(
        module_key=module_key,
        module_label=module_label,
        section_key=section_key,
        section_label=section_label,
        content=filled_content,
        prompt_messages=serialised_messages,
    )
