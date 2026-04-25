"""writer/data_analyst.py

Data Analyst Agent — Option C (hybrid).

Sits between clinical_reader and generator. Given a pandas DataFrame (loaded
from one CSV) and the placeholder keys that need values from that DataFrame,
it uses a small set of pre-coded, deterministic statistical tools.  The LLM
acts only as a dispatcher: it reads each placeholder's description and calls
the appropriate tool with the right column / group-by arguments.

No free Python/pandas code is generated or executed — the LLM can only call
the four typed tools defined here.  All arithmetic is done by pandas in the
tool implementations.

Tools
─────
  compute_proportion   – count(condition) / N, optionally by group
  compute_mean_sd      – mean ± SD of a numeric column, optionally by group
  compute_crosstab     – cross-tabulation of two categorical columns (n and %)
  compute_median_range – median [min–max] of a numeric column, optionally by group

Public API
──────────
  analyse_dataframe(df, placeholder_descriptions, llm) → dict[str, str]
    Returns a mapping  placeholder_key → human-readable computed string,
    ready to be injected as clinical context for the writer LLM.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

import pandas as pd
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# ── Statistical tools (deterministic — no LLM code generation) ───────────────

@tool
def compute_proportion(
    data_json: Annotated[str, "JSON array of row dicts (the relevant CSV rows)"],
    column: Annotated[str, "Column name to compute proportion for"],
    positive_value: Annotated[str, "Value that counts as 'positive' / 'Yes' / 'True'"],
    group_by: Annotated[str, "Column to group by, or empty string for overall"] = "",
) -> str:
    """Compute the proportion (%) of rows where column == positive_value.

    Returns a formatted string like '71.2% (127/178)' or a group breakdown
    table when group_by is provided.
    """
    rows = json.loads(data_json)
    df = pd.DataFrame(rows)
    if column not in df.columns:
        return f"[column '{column}' not found]"

    if group_by and group_by in df.columns:
        lines: list[str] = []
        for grp, sub in df.groupby(group_by):
            n_pos = (sub[column].astype(str).str.strip() == str(positive_value)).sum()
            n_tot = len(sub)
            pct = 100 * n_pos / n_tot if n_tot else 0
            lines.append(f"  {grp}: {pct:.1f}% ({n_pos}/{n_tot})")
        return "\n".join(lines)
    else:
        n_pos = (df[column].astype(str).str.strip() == str(positive_value)).sum()
        n_tot = len(df)
        pct = 100 * n_pos / n_tot if n_tot else 0
        return f"{pct:.1f}% ({n_pos}/{n_tot})"


@tool
def compute_mean_sd(
    data_json: Annotated[str, "JSON array of row dicts (the relevant CSV rows)"],
    column: Annotated[str, "Numeric column name to compute mean ± SD for"],
    group_by: Annotated[str, "Column to group by, or empty string for overall"] = "",
) -> str:
    """Compute mean ± SD of a numeric column, optionally stratified by group.

    Returns a string like '2.1 ± 0.8' or a group breakdown table.
    """
    rows = json.loads(data_json)
    df = pd.DataFrame(rows)
    if column not in df.columns:
        return f"[column '{column}' not found]"

    df[column] = pd.to_numeric(df[column], errors="coerce")

    if group_by and group_by in df.columns:
        lines: list[str] = []
        for grp, sub in df.groupby(group_by):
            m = sub[column].mean()
            s = sub[column].std()
            lines.append(f"  {grp}: {m:.2f} ± {s:.2f} (n={sub[column].notna().sum()})")
        return "\n".join(lines)
    else:
        m = df[column].mean()
        s = df[column].std()
        return f"{m:.2f} ± {s:.2f} (n={df[column].notna().sum()})"


@tool
def compute_crosstab(
    data_json: Annotated[str, "JSON array of row dicts (the relevant CSV rows)"],
    row_column: Annotated[str, "Column whose unique values become rows"],
    col_column: Annotated[str, "Column whose unique values become columns"],
) -> str:
    """Cross-tabulate two categorical columns and return counts + row percentages.

    Returns a Markdown table string.
    """
    rows = json.loads(data_json)
    df = pd.DataFrame(rows)
    if row_column not in df.columns or col_column not in df.columns:
        missing = [c for c in [row_column, col_column] if c not in df.columns]
        return f"[columns not found: {missing}]"

    ct = pd.crosstab(df[row_column], df[col_column])
    ct_pct = ct.div(ct.sum(axis=1), axis=0).mul(100).round(1)

    col_headers = " | ".join(str(c) for c in ct.columns)
    lines = [f"| {row_column} | {col_headers} |",
             "|" + "---|" * (len(ct.columns) + 1)]
    for idx in ct.index:
        cells = " | ".join(
            f"{ct.loc[idx, c]} ({ct_pct.loc[idx, c]:.1f}%)" for c in ct.columns
        )
        lines.append(f"| {idx} | {cells} |")
    return "\n".join(lines)


@tool
def compute_median_range(
    data_json: Annotated[str, "JSON array of row dicts (the relevant CSV rows)"],
    column: Annotated[str, "Numeric column name to compute median [min–max] for"],
    group_by: Annotated[str, "Column to group by, or empty string for overall"] = "",
) -> str:
    """Compute median [min–max] of a numeric column, optionally by group.

    Returns a string like '3.0 [1–6]' or a group breakdown table.
    """
    rows = json.loads(data_json)
    df = pd.DataFrame(rows)
    if column not in df.columns:
        return f"[column '{column}' not found]"

    df[column] = pd.to_numeric(df[column], errors="coerce")

    if group_by and group_by in df.columns:
        lines: list[str] = []
        for grp, sub in df.groupby(group_by):
            med = sub[column].median()
            mn, mx = sub[column].min(), sub[column].max()
            lines.append(f"  {grp}: {med:.1f} [{mn:.0f}–{mx:.0f}] (n={sub[column].notna().sum()})")
        return "\n".join(lines)
    else:
        med = df[column].median()
        mn, mx = df[column].min(), df[column].max()
        return f"{med:.1f} [{mn:.0f}–{mx:.0f}] (n={df[column].notna().sum()})"


# ── Tool registry ─────────────────────────────────────────────────────────────

_TOOLS = [compute_proportion, compute_mean_sd, compute_crosstab, compute_median_range]
_TOOL_MAP = {t.name: t for t in _TOOLS}

# ── Dispatcher prompt ─────────────────────────────────────────────────────────

_ANALYST_SYSTEM_TMPL = """\
You are a clinical data analyst.  You have access to four statistical tools:

  compute_proportion   – proportion / rate / recovery rate / response rate
  compute_mean_sd      – mean ± SD of a numeric measurement
  compute_crosstab     – cross-table of two categorical columns (e.g. treatment × outcome)
  compute_median_range – median [min–max] for skewed distributions (e.g. age, time-to-event)

Rules:
- Call EXACTLY ONE tool per placeholder key listed below.
- Choose the tool that best matches the clinical meaning of the placeholder.
- For proportions (recovery rate, response rate, incidence) → compute_proportion.
- For continuous measurements (score, age, weight) → compute_mean_sd or compute_median_range.
- For two-way breakdowns (treatment group × outcome) → compute_crosstab.
- The dataset-specific treatment/arm grouping column is: {group_col}.
  Use it as group_by when a by-treatment breakdown is clinically meaningful.
  If it is empty or not applicable, omit group_by (leave it as the default empty string).
- Only use column names that appear in the "Dataset columns available" list below.
- The data_json argument must be a valid JSON array of row objects — use the
  EXACT sample rows provided in the user message, not synthetic data.
- Do not invent or modify values.
"""

# Keywords that suggest a column is a treatment / arm / group identifier
_GROUP_HINTS = ["treatment", "arm", "group", "intervention", "randomiz", "cohort", "stratum"]


def _detect_group_column(df: pd.DataFrame) -> str:
    """Return the most likely treatment/arm grouping column, or empty string."""
    for col in df.columns:
        if any(hint in col.lower() for hint in _GROUP_HINTS):
            return col
    return ""

_ANALYST_USER_TMPL = """\
Dataset columns available: {columns}

Sample rows (up to 20):
{sample_rows_json}

For each placeholder below, call the appropriate tool to compute the value.
Work through them one by one.

Placeholders to compute:
{placeholder_list}
"""


def analyse_dataframe(
    df: pd.DataFrame,
    placeholder_descriptions: dict[str, str],
    llm: ChatOpenAI,
) -> dict[str, str]:
    """Dispatch statistical tools via LLM tool-calling to compute placeholder values.

    Args:
        df:                        Full DataFrame loaded from one CSV source.
        placeholder_descriptions:  Mapping of placeholder_key → description string
                                   (from the ClinicalDataManifest column_mappings).
        llm:                       Bound ChatOpenAI instance (must support tool-calling).

    Returns:
        dict[placeholder_key → computed_string] — ready for writer context injection.
    """
    if df.empty or not placeholder_descriptions:
        return {}

    # Pass up to 20 rows as sample data in the prompt (keeps token cost low)
    sample = df.head(20)
    sample_json = sample.to_json(orient="records", indent=2)
    columns = list(df.columns)

    placeholder_list = "\n".join(
        f"  - {key}: {desc}" for key, desc in placeholder_descriptions.items()
    )

    group_col = _detect_group_column(df)
    analyst_system = _ANALYST_SYSTEM_TMPL.format(
        group_col=group_col if group_col else "(none detected — omit group_by)"
    )

    messages = [
        SystemMessage(content=analyst_system),
        HumanMessage(content=_ANALYST_USER_TMPL.format(
            columns=columns,
            sample_rows_json=sample_json,
            placeholder_list=placeholder_list,
        )),
    ]

    llm_with_tools = llm.bind_tools(_TOOLS)
    results: dict[str, str] = {}

    try:
        response = llm_with_tools.invoke(messages)
    except Exception as exc:
        logger.warning("[data_analyst] LLM dispatch failed: %s", exc)
        return {}

    # Execute each tool call deterministically
    full_data_json = df.to_json(orient="records")
    assigned_keys: set[str] = set()

    for tool_call in getattr(response, "tool_calls", []):
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]

        if tool_name not in _TOOL_MAP:
            logger.warning("[data_analyst] Unknown tool requested: %s", tool_name)
            continue

        # Replace sample_rows_json with full-dataset JSON for accuracy
        # (the LLM echoes back the sample rows — we substitute the full df)
        tool_args = {**tool_args, "data_json": full_data_json}

        try:
            result_str = _TOOL_MAP[tool_name].invoke(tool_args)
        except Exception as exc:
            logger.warning("[data_analyst] Tool %s failed: %s", tool_name, exc)
            continue

        # Map result back to the placeholder key using the column name.
        # Descriptions are now the CSV column names so we can do exact matching.
        matched_key = _match_placeholder(tool_args, placeholder_descriptions, assigned_keys)
        if matched_key:
            results[matched_key] = result_str
            assigned_keys.add(matched_key)
            logger.info("[data_analyst] %s → %s", matched_key, result_str[:80])

    return results


def _match_placeholder(
    tool_args: dict,
    placeholder_descriptions: dict[str, str],
    assigned_keys: set[str] | None = None,
) -> str | None:
    """Map a tool call back to a placeholder key using the CSV column name.

    Since ``placeholder_descriptions`` values are now the actual CSV column
    names (not role labels), we can do reliable exact + partial matching.

    ``assigned_keys`` prevents multiple tool calls from overwriting the same
    placeholder key when the LLM makes duplicate or ambiguous calls.
    """
    column = tool_args.get("column") or tool_args.get("row_column") or ""
    col_lower = column.lower().strip()

    def _available(key: str) -> bool:
        return assigned_keys is None or key not in assigned_keys

    if col_lower:
        # Pass 1: exact case-insensitive match — description IS the column name
        for key, desc in placeholder_descriptions.items():
            if _available(key) and col_lower == desc.lower().strip():
                return key

        # Pass 2: partial overlap (column name is a substring of description or vice versa)
        for key, desc in placeholder_descriptions.items():
            if _available(key):
                desc_lower = desc.lower().strip()
                if col_lower in desc_lower or desc_lower in col_lower:
                    return key

    # Fallback: first unassigned key
    for key in placeholder_descriptions:
        if _available(key):
            return key
    return None
