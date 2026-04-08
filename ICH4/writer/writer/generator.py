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
You are a senior pharmaceutical regulatory scientist writing CTD submission content.

Your task: fill every {{placeholder}} in the provided template with accurate,
programme-specific regulatory prose based on the clinical data excerpts supplied.

Rules:
- Replace EVERY {{placeholder}} with substantive content.
- Base all efficacy and safety statements on the clinical data provided.
- If clinical data for a placeholder is absent, write: "[DATA PENDING — author to supply]"
- Keep language formal, regulatory-appropriate, and consistent with ICH M4 requirements.
- Do NOT alter section headings, tables, or structural elements — only fill placeholders.
- Return ONLY the completed Markdown. No commentary, no JSON wrapping.
"""

_USER_TMPL = """\
Program: {drug_name} | {disease_type} | {therapeutic_area}
Section: {section_key} — {section_label}

=== TEMPLATE TO FILL ===
{template_content}

=== CLINICAL DATA FOR PLACEHOLDERS ===
{clinical_context}

Fill every {{{{placeholder}}}} in the template above using the clinical data.
Return the completed Markdown document only.
"""


def _fmt_clinical_context(context: dict[str, str]) -> str:
    if not context:
        return "No clinical data available — use [DATA PENDING — author to supply] for all placeholders."
    parts: list[str] = []
    for key, data in context.items():
        parts.append(f"--- {{{{  {key}  }}}} ---\n{data}")
    return "\n\n".join(parts)


def write_section(
    program: ProgramInfo,
    section_key: str,
    section_label: str,
    module_key: str,
    module_label: str,
    template_content: str,
    bucket_name: str,
    llm: ChatOpenAI,
) -> SectionDocument:
    """Fill one template and return a SectionDocument with completed prose."""
    placeholders = extract_placeholders(template_content)
    logger.info("[writer] Section %s — %d placeholders: %s",
                section_key, len(placeholders), placeholders)

    clinical_ctx = build_clinical_context(bucket_name, program, placeholders, llm=llm)

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=_USER_TMPL.format(
            drug_name=program.drug_name,
            disease_type=program.disease_type,
            therapeutic_area=program.therapeutic_area,
            section_key=section_key,
            section_label=section_label,
            template_content=template_content,
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
            {k: "[DATA PENDING — author to supply]" for k in remaining},
        )

    return SectionDocument(
        module_key=module_key,
        module_label=module_label,
        section_key=section_key,
        section_label=section_label,
        content=filled_content,
    )
