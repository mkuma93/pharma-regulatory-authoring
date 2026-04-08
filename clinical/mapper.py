"""
clinical/mapper.py

LLM-based mapper: reads CSV headers + sample rows and returns a ClinicalDataSource
with column roles, CTD section keys, and {{placeholder}} names.

One LLM call per CSV.  Uses gpt-4o-mini by default (cheap, fast, sufficient for
structured column classification).
"""
from __future__ import annotations

import csv
import io
import json
import os

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from template.models import ProgramInfo

from .models import ColumnMapping, ClinicalDataSource

# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a senior research scientist in the pharmaceutical industry with deep expertise \
in clinical drug development, biostatistics, and ICH CTD regulatory submissions. \
You have personally designed Phase II/III trials, reviewed clinical study reports, \
and mapped raw clinical datasets to IND, NDA, and MAA dossiers across multiple \
therapeutic areas.

Your task is to analyse a clinical dataset and map each column to its regulatory \
role and the CTD sections where its data would appear in a submission. \
Think like a scientist preparing data for a regulatory reviewer — every column \
you classify must have a clear, defensible reason to appear in a specific CTD section.

ICH M4 section key conventions (use short numeric keys only):
  Module 2 summaries : 2.5 (clinical overview), 2.7.1–2.7.6 (clinical summary subsections)
  Module 5 study data: 5.2 (tabular listing), 5.3.5.1 (controlled trials),
                       5.3.5.2 (uncontrolled trials), 5.3.7 (other reports), 5.4 (literature)

Placeholder naming rules:
  - Use snake_case
  - Be specific: 'recovery_rate_3mo' rather than 'rate'
  - Include units or time points when relevant, e.g. 'median_enrollment_count', 'n_rct_studies'

Return ONLY a valid JSON object — no prose, no markdown fences.
"""

_MAPPER_PROMPT = """\
Dataset context
───────────────
  Program          : {drug_name} / {disease_type} / {therapeutic_area}
  Filename         : {filename}
  Total rows       : {n_rows}

Column headers with up to {n_sample} sample values each
────────────────────────────────────────────────────────
{column_samples}

Task
────
1. Determine study_type: "RCT" | "registry" | "observational" | "PK" | "safety" | "other"

2. For EACH column relevant to a CTD submission, produce a ColumnMapping:
     column_name      – exact header string
     role             – one of: "efficacy_endpoint" | "safety_endpoint" | "demographics" |
                                "treatment_group" | "study_metadata" | "pharmacokinetics" | "other"
     ctd_section_keys – list of ICH M4 short section keys (e.g. ["5.3.5.1", "2.7.3"])
     placeholder_key  – snake_case name for {{placeholder}} use in templates

   Skip columns that carry no regulatory meaning (e.g. row IDs, internal codes with no
   CTD relevance).

3. Set the top-level ctd_section_keys as the sorted union of all column ctd_section_keys.

Return a JSON object with exactly this shape:
{{
  "filename": "{filename}",
  "gcs_path": "",
  "study_type": "...",
  "column_mappings": [
    {{
      "column_name": "...",
      "role": "...",
      "ctd_section_keys": ["..."],
      "placeholder_key": "..."
    }}
  ],
  "ctd_section_keys": ["..."]
}}
"""

# ── Public API ────────────────────────────────────────────────────────────────

def map_csv_to_ctd(
    filename: str,
    csv_content: str,
    program: ProgramInfo,
    n_sample_rows: int = 5,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
) -> ClinicalDataSource:
    """Send CSV headers + sample rows to the LLM and return a ClinicalDataSource.

    Args:
        filename:      CSV filename (used for display and LLM context).
        csv_content:   Full CSV text content.
        program:       Drug program context.
        n_sample_rows: Number of sample rows to show per column in the prompt.
        api_key:       OpenAI API key (falls back to OPENAI_API_KEY env var).
        model:         OpenAI model to use.

    Returns:
        :class:`ClinicalDataSource` with column_mappings and ctd_section_keys populated.
        ``gcs_path`` is left as an empty string — set by the caller after GCS upload.
    """
    effective_key = api_key or os.environ.get("OPENAI_API_KEY")
    llm = ChatOpenAI(model=model, temperature=0.0, api_key=effective_key)

    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)
    headers: list[str] = list(reader.fieldnames or [])

    # Build column → [sample values] map
    col_samples: dict[str, list[str]] = {h: [] for h in headers}
    for row in rows[:n_sample_rows]:
        for h in headers:
            val = str(row.get(h, "")).strip()
            col_samples[h].append(val if val else "(empty)")

    column_samples_text = "\n".join(
        f"  {h!r}: {col_samples[h]}" for h in headers
    )

    prompt = _MAPPER_PROMPT.format(
        drug_name=program.drug_name,
        disease_type=program.disease_type,
        therapeutic_area=program.therapeutic_area,
        filename=filename,
        n_rows=len(rows),
        n_sample=n_sample_rows,
        column_samples=column_samples_text,
    )

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    response = llm.invoke(messages)
    raw = response.content.strip()

    # Strip markdown code fences if the LLM wraps the JSON
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    data = json.loads(raw)

    column_mappings = [ColumnMapping(**cm) for cm in data.get("column_mappings", [])]

    # Always recompute ctd_section_keys as the union — don't trust LLM exactly
    all_section_keys: set[str] = set()
    for cm in column_mappings:
        all_section_keys.update(cm.ctd_section_keys)

    return ClinicalDataSource(
        filename=filename,
        gcs_path=data.get("gcs_path", ""),
        study_type=data.get("study_type", "other"),
        column_mappings=column_mappings,
        ctd_section_keys=sorted(all_section_keys),
    )
