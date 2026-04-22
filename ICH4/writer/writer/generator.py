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
Fill every {{placeholder}} in the template provided using ONLY the clinical evidence
supplied in the "CLINICAL DATA FOR PLACEHOLDERS" section below.

STRICT EVIDENCE RULES — THESE ARE NON-NEGOTIABLE
─────────────────────────────────────────────────
1. EVIDENCE-ONLY FIGURES
   Every numeric value (N, %, mean, p-value, OR, CI, NNT, AE rate, etc.)
   MUST come verbatim from the supplied clinical data.
   ► If no clinical data supports a numeric placeholder → write:
       [DATA PENDING — {placeholder_key}: no source data supplied]
   ► NEVER invent, estimate, or extrapolate a number.

2. NO-HALLUCINATION FOR SPECIFIC CLAIMS
   Do NOT state that drug X produced a specific efficacy result, safety rate,
   or demographic stat unless that exact value appears in the supplied data.
   ► Unsupported specific claims are a regulatory integrity violation.

3. REGULATORY CONTEXT PLACEHOLDERS
   For placeholders describing regulatory process, pharmacology mechanism,
   or study design rationale (not numeric outcomes), you MAY write concise
   evidence-informed prose consistent with the drug class and indication.
   ► Clearly distinguish mechanism/context prose from outcome data.
   ► Avoid superlatives ("best-in-class", "superior", "revolutionary").

4. NARRATIVE PLACEHOLDERS WITH PARTIAL DATA
   If data is available for some — but not all — sub-points of a narrative
   placeholder, write the data-supported sentences first, then append:
       [DATA PENDING — remaining content: author to supply with study references]

5. CROSS-REFERENCE REQUIREMENT
   For every efficacy or safety figure you include, append an inline source tag:
       (Source: {filename}, {column_name})
   where filename and column_name come from the supplied clinical data header.
   If you cannot tag a figure with a source, it must be [DATA PENDING].

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
REMINDER: Fill ONLY with values traceable to the clinical data above.
Any numeric value without a source in the data above MUST be written as:
  [DATA PENDING — {{placeholder_key}}: no source data supplied]
Do NOT invent or estimate figures. Return completed Markdown only.
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
            f"--- Source for {{{{  {key}  }}}} ---\n"
            f"(Use ONLY the figures below for this placeholder)\n"
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

    return SectionDocument(
        module_key=module_key,
        module_label=module_label,
        section_key=section_key,
        section_label=section_label,
        content=filled_content,
    )
