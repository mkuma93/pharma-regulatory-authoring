"""Load clinical CSV data from GCS and build context strings for placeholder filling.

Uses the ClinicalDataManifest (written by the template service) to map
CSV columns → placeholder keys.  Raw rows are passed through the DataAnalyst
agent which computes deterministic statistical summaries (proportions, mean ± SD,
crosstabs, median [range]) before handing results to the writer LLM.
"""
from __future__ import annotations

import io
import logging
from csv import DictReader

import pandas as pd
from langchain_openai import ChatOpenAI

from .data_analyst import analyse_dataframe
from .gcs_client import gcs, program_prefix
from .models import ProgramInfo

logger = logging.getLogger(__name__)


def load_clinical_manifest(bucket_name: str, program: ProgramInfo) -> dict | None:
    """Return the parsed clinical_data/manifest.json dict, or None if absent."""
    prefix = program_prefix(program)
    path   = f"{prefix}/clinical_data/manifest.json"
    try:
        bkt  = gcs().bucket(bucket_name)
        blob = bkt.blob(path)
        if not blob.exists():
            logger.info("[clinical_reader] No manifest at %s", path)
            return None
        import json
        return json.loads(blob.download_as_text())
    except Exception as exc:
        logger.warning("[clinical_reader] Failed to load manifest: %s", exc)
        return None


def _csv_rows_for_columns(
    bucket_name: str,
    gcs_path: str,
    column_names: list[str],
    max_rows: int | None = 50,
) -> list[dict[str, str]]:
    """Download a CSV from GCS and return up to *max_rows* rows (None = all),
    keeping only the specified *column_names*."""
    try:
        bkt  = gcs().bucket(bucket_name)
        blob = bkt.blob(gcs_path.lstrip("gs://").split("/", 1)[-1]
                        if gcs_path.startswith("gs://") else gcs_path)
        text    = blob.download_as_text(encoding="utf-8")
        reader  = DictReader(io.StringIO(text))
        rows: list[dict[str, str]] = []
        for row in reader:
            filtered = {c: row[c] for c in column_names if c in row}
            if filtered:
                rows.append(filtered)
            if max_rows is not None and len(rows) >= max_rows:
                break
        return rows
    except Exception as exc:
        logger.warning("[clinical_reader] Failed to read CSV %s: %s", gcs_path, exc)
        return []


def build_clinical_context(
    bucket_name: str,
    program: ProgramInfo,
    placeholder_keys: list[str],
    llm: ChatOpenAI | None = None,
) -> dict[str, str]:
    """Return a mapping  placeholder_key → computed statistical summary string.

    For each CSV source in the manifest whose columns map to the requested
    placeholder_keys, loads the full DataFrame, runs the DataAnalyst agent to
    compute deterministic summaries (proportions, mean ± SD, median [range],
    crosstabs), and returns the results.

    Falls back to raw row strings if no LLM is provided (test / offline mode).
    Returns empty dict if no manifest is found.
    """
    manifest = load_clinical_manifest(bucket_name, program)
    if not manifest:
        return {}

    # Build lookup: placeholder_key → {column_name, gcs_path, description}
    placeholder_meta: dict[str, dict] = {}
    for source in manifest.get("sources", []):
        gcs_path = source.get("gcs_path", "")
        for mapping in source.get("column_mappings", []):
            pk = mapping.get("placeholder_key", "")
            cn = mapping.get("column_name", "")
            if pk and cn and pk in placeholder_keys:
                placeholder_meta[pk] = {
                    "column_name": cn,
                    "gcs_path": gcs_path,
                    "role": mapping.get("role", ""),
                    "description": f"{mapping.get('role', '')} — column: {cn}",
                }

    if not placeholder_meta:
        return {}

    # Group by CSV file so we load each file once
    file_groups: dict[str, dict[str, dict]] = {}
    for pk, meta in placeholder_meta.items():
        file_groups.setdefault(meta["gcs_path"], {})[pk] = meta

    context: dict[str, str] = {}

    for gcs_path, ph_group in file_groups.items():
        # Load all columns needed for this file
        columns_needed = list({m["column_name"] for m in ph_group.values()})
        rows = _csv_rows_for_columns(bucket_name, gcs_path, columns_needed,
                                     max_rows=None)  # load full dataset for accuracy
        if not rows:
            continue

        df = pd.DataFrame(rows)

        if llm is not None:
            # Option C: tool-calling analyst computes deterministic summaries
            ph_descriptions = {pk: m["description"] for pk, m in ph_group.items()}
            computed = analyse_dataframe(df, ph_descriptions, llm)
            context.update(computed)
        else:
            # Fallback: raw row strings (for tests / offline mode)
            for pk, meta in ph_group.items():
                col = meta["column_name"]
                col_rows = df[[col]].dropna().head(20)
                context[pk] = (
                    f"Source: {gcs_path.rsplit('/', 1)[-1]}\n"
                    + col_rows.to_string(index=False)
                )

    return context
