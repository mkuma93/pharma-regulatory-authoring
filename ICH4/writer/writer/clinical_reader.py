"""Load clinical CSV data from GCS and build context strings for placeholder filling.

Uses the ClinicalDataManifest (written by the template service) to map
CSV columns → placeholder keys.  Pre-resolved values from clinical-analyst /resolve
are used directly; any unresolved keys fall back to raw row strings so the
writer LLM can write [DATA PENDING] rather than hallucinating figures.

Data computation (deterministic statistics) is owned entirely by clinical-analyst.
"""
from __future__ import annotations

import io
import logging
from csv import DictReader
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
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
    resolved_values: dict[str, str] | None = None,
    llm: ChatOpenAI | None = None,
) -> dict[str, str]:
    """Return a mapping  placeholder_key → value string for the writer LLM.

    Seeds from *resolved_values* (pre-computed by clinical-analyst /resolve).
    For unresolved keys, runs ``data_analyst.analyse_dataframe()`` (when *llm*
    is supplied) to compute real statistics (proportions, mean±SD, etc.) from
    the clinical CSV via safe LLM tool-calling — no arbitrary code execution.
    Falls back to raw column row snippets when no LLM is provided.
    Returns empty dict if no manifest is found.
    """
    # Start with pre-resolved values; hybrid analyst fills whatever is missing
    context: dict[str, str] = dict(resolved_values or {})

    # Only dispatch analyst for keys not already resolved upstream
    remaining_keys = [k for k in placeholder_keys if k not in context]
    if not remaining_keys:
        return context

    manifest = load_clinical_manifest(bucket_name, program)
    if not manifest:
        return context

    # Build lookup: placeholder_key → list of {column_name, gcs_path, role}
    # A placeholder may map to MULTIPLE columns (aggregate/narrative keys).
    placeholder_meta: dict[str, list[dict]] = {}
    for source in manifest.get("sources", []):
        gcs_path = source.get("gcs_path", "")
        for mapping in source.get("column_mappings", []):
            pk = mapping.get("placeholder_key", "")
            cn = mapping.get("column_name", "")
            if pk and cn and pk in remaining_keys:
                placeholder_meta.setdefault(pk, []).append({
                    "column_name": cn,
                    "gcs_path": gcs_path,
                    "role": mapping.get("role", ""),
                })

    if not placeholder_meta:
        # Fallback: inject resolved_values as context for any remaining narrative keys
        if resolved_values:
            stats_block = "Pre-resolved clinical statistics:\n" + "\n".join(
                f"  {k}: {v}" for k, v in resolved_values.items()
            )
            for pk in remaining_keys:
                context[pk] = stats_block
        return context

    # Group by CSV file so we load each file once
    # Maps gcs_path → set of column names needed from that file
    file_columns: dict[str, set[str]] = {}
    for col_list in placeholder_meta.values():
        for entry in col_list:
            file_columns.setdefault(entry["gcs_path"], set()).add(entry["column_name"])

    # Load each CSV file once
    file_dfs: dict[str, pd.DataFrame] = {}
    for gcs_path, cols in file_columns.items():
        rows = _csv_rows_for_columns(bucket_name, gcs_path, list(cols), max_rows=None)
        if rows:
            file_dfs[gcs_path] = pd.DataFrame(rows)

    filename_short: dict[str, str] = {p: p.rsplit("/", 1)[-1] for p in file_dfs}

    # ── data_analyst: compute real statistics when LLM is available ──────────
    # Groups placeholder_descriptions by CSV file so each file is analysed once.
    if llm is not None:
        for gcs_path, df in file_dfs.items():
            # Collect placeholder keys whose source is this CSV file
            ph_descs: dict[str, str] = {}
            for pk, col_list in placeholder_meta.items():
                if pk in context:
                    continue  # already resolved upstream
                for entry in col_list:
                    if entry["gcs_path"] == gcs_path:
                        desc = entry.get("role") or entry["column_name"]
                        ph_descs[pk] = desc
                        break
            if ph_descs:
                try:
                    computed = analyse_dataframe(df, ph_descs, llm)
                    context.update(computed)
                    logger.info(
                        "[clinical_reader] data_analyst computed %d value(s) from %s",
                        len(computed), filename_short.get(gcs_path, gcs_path),
                    )
                except Exception as exc:
                    logger.warning(
                        "[clinical_reader] data_analyst failed for %s: %s — falling back",
                        filename_short.get(gcs_path, gcs_path), exc,
                    )

    # Build context: accumulate raw column snippets for any keys still missing
    for pk, col_list in placeholder_meta.items():
        if pk in context:
            continue  # already computed by data_analyst above
        parts: list[str] = []
        for entry in col_list:
            gp = entry["gcs_path"]
            col = entry["column_name"]
            df = file_dfs.get(gp)
            if df is None or col not in df.columns:
                continue
            col_rows = df[[col]].dropna().head(20)
            parts.append(
                f"Column '{col}' from {filename_short.get(gp, gp)}:\n"
                + col_rows.to_string(index=False)
            )
        if parts:
            context[pk] = "\n\n".join(parts)

    # Fallback for any remaining_keys still not covered: inject resolved_values
    if resolved_values:
        stats_block = "Pre-resolved clinical statistics:\n" + "\n".join(
            f"  {k}: {v}" for k, v in resolved_values.items()
        )
        for pk in remaining_keys:
            if pk not in context:
                context[pk] = stats_block

    return context
