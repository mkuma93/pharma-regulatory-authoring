"""
clinical-analyst/app.py

Clinical Data Analyst Service — FastAPI.

Responsibilities
────────────────
  • Read clinical CSV files from GCS using the standard clinical_data/manifest.json layout.
  • Run statistical analysis with pandas (demographics, efficacy endpoints, safety).
  • Use GPT-4o to synthesise narrative insights and answer free-form questions.
  • Return structured results that the UI can display directly in the chat.

GCS path conventions (shared with writer service)
──────────────────────────────────────────────────
  {bucket}/therapeutic-area/{ta}/{disease}/{drug}/
    clinical_data/
      manifest.json                ← lists CSV paths + column mappings
      {filename}.csv               ← uploaded clinical trial data

Endpoints
─────────
  GET  /health   — liveness probe
  POST /analyze  — full structured analysis for a program
  POST /query    — free-form natural language Q&A against the clinical data
"""
from __future__ import annotations

import io
import json
import logging
import os
from typing import Annotated, Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from google.cloud import storage
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_BUCKET = os.environ.get("GCS_BUCKET", "pharma-reguatory-author-life-science")
_LLM_MODEL      = os.environ.get("LLM_MODEL", "gpt-4o-mini")
_MAX_ROWS       = int(os.environ.get("MAX_ANALYSIS_ROWS", "1000"))

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Clinical Analyst Service",
    version="1.0.0",
    description="Clinical data analysis and Q&A for regulatory authoring.",
)


# ── Request / Response models ─────────────────────────────────────────────────

class ResolveRequest(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str
    bucket: str | None = Field(default=None)
    placeholder_keys: list[str] = Field(
        default_factory=list,
        description="Keys to resolve. Empty = resolve all keys found in manifest.",
    )


class ResolveResponse(BaseModel):
    resolved_values: dict[str, str]
    saved_path: str
    keys_resolved: int
    keys_failed: list[str] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str
    bucket: str | None = Field(default=None)
    question: str | None = Field(
        default=None,
        description="Optional: extra question to include in the analysis summary.",
    )


class QueryRequest(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str
    bucket: str | None = Field(default=None)
    question: str = Field(description="Free-form question about the clinical data.")


class AnalysisResult(BaseModel):
    reply: str = Field(description="Markdown narrative for the chat interface.")
    state_patch: dict = Field(default_factory=dict)
    stats: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw statistical summaries keyed by source filename.",
    )


# ── Resolve: deterministic statistical tools (mirrors writer/data_analyst.py) ──

@tool
def compute_proportion(
    data_json: Annotated[str, "JSON array of row dicts"],
    column: Annotated[str, "Column to compute proportion for"],
    positive_value: Annotated[str, "Value that counts as positive"],
    group_by: Annotated[str, "Column to group by, or empty string"] = "",
) -> str:
    """Compute proportion (%) of rows where column == positive_value."""
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
    n_pos = (df[column].astype(str).str.strip() == str(positive_value)).sum()
    n_tot = len(df)
    pct = 100 * n_pos / n_tot if n_tot else 0
    return f"{pct:.1f}% ({n_pos}/{n_tot})"


@tool
def compute_mean_sd(
    data_json: Annotated[str, "JSON array of row dicts"],
    column: Annotated[str, "Numeric column name"],
    group_by: Annotated[str, "Column to group by, or empty string"] = "",
) -> str:
    """Compute mean ± SD of a numeric column, optionally by group."""
    rows = json.loads(data_json)
    df = pd.DataFrame(rows)
    if column not in df.columns:
        return f"[column '{column}' not found]"
    df[column] = pd.to_numeric(df[column], errors="coerce")
    if group_by and group_by in df.columns:
        lines: list[str] = []
        for grp, sub in df.groupby(group_by):
            m, s = sub[column].mean(), sub[column].std()
            lines.append(f"  {grp}: {m:.2f} ± {s:.2f} (n={sub[column].notna().sum()})")
        return "\n".join(lines)
    m, s = df[column].mean(), df[column].std()
    return f"{m:.2f} ± {s:.2f} (n={df[column].notna().sum()})"


@tool
def compute_crosstab(
    data_json: Annotated[str, "JSON array of row dicts"],
    row_column: Annotated[str, "Column whose values become rows"],
    col_column: Annotated[str, "Column whose values become columns"],
) -> str:
    """Cross-tabulate two categorical columns, return Markdown table."""
    rows = json.loads(data_json)
    df = pd.DataFrame(rows)
    missing = [c for c in [row_column, col_column] if c not in df.columns]
    if missing:
        return f"[columns not found: {missing}]"
    ct = pd.crosstab(df[row_column], df[col_column])
    ct_pct = ct.div(ct.sum(axis=1), axis=0).mul(100).round(1)
    col_headers = " | ".join(str(c) for c in ct.columns)
    lines = [f"| {row_column} | {col_headers} |", "|" + "---|" * (len(ct.columns) + 1)]
    for idx in ct.index:
        cells = " | ".join(
            f"{ct.loc[idx, c]} ({ct_pct.loc[idx, c]:.1f}%)" for c in ct.columns
        )
        lines.append(f"| {idx} | {cells} |")
    return "\n".join(lines)


@tool
def compute_median_range(
    data_json: Annotated[str, "JSON array of row dicts"],
    column: Annotated[str, "Numeric column name"],
    group_by: Annotated[str, "Column to group by, or empty string"] = "",
) -> str:
    """Compute median [min–max] of a numeric column, optionally by group."""
    rows = json.loads(data_json)
    df = pd.DataFrame(rows)
    if column not in df.columns:
        return f"[column '{column}' not found]"
    df[column] = pd.to_numeric(df[column], errors="coerce")
    if group_by and group_by in df.columns:
        lines: list[str] = []
        for grp, sub in df.groupby(group_by):
            med, mn, mx = sub[column].median(), sub[column].min(), sub[column].max()
            lines.append(f"  {grp}: {med:.1f} [{mn:.0f}\u2013{mx:.0f}] (n={sub[column].notna().sum()})")
        return "\n".join(lines)
    med, mn, mx = df[column].median(), df[column].min(), df[column].max()
    return f"{med:.1f} [{mn:.0f}\u2013{mx:.0f}] (n={df[column].notna().sum()})"


_RESOLVE_TOOLS    = [compute_proportion, compute_mean_sd, compute_crosstab, compute_median_range]
_RESOLVE_TOOL_MAP = {t.name: t for t in _RESOLVE_TOOLS}
_GROUP_HINTS_RESOLVE = ["treatment", "arm", "group", "intervention", "randomiz", "cohort", "stratum"]

_RESOLVE_SYSTEM = """\
You are a clinical data analyst. You have access to four statistical tools:
  compute_proportion   – proportion / rate / recovery rate / response rate
  compute_mean_sd      – mean ± SD of a numeric measurement
  compute_crosstab     – cross-table of two categorical columns
  compute_median_range – median [min–max] for skewed distributions

Rules:
- Call EXACTLY ONE tool per placeholder key listed below.
- For proportions (rate, incidence, response) → compute_proportion.
- For continuous measurements (score, age, weight) → compute_mean_sd or compute_median_range.
- For two-way breakdowns → compute_crosstab.
- The treatment/arm grouping column is: {group_col}.
  Use it as group_by when clinically meaningful. If empty, omit group_by.
- Only use column names from the "Dataset columns available" list.
- The data_json must be a valid JSON array of row objects from the sample rows provided.
- Do not invent or modify values.
"""

_RESOLVE_USER = """\
Dataset columns available: {columns}

Sample rows (up to 20):
{sample_rows_json}

For each placeholder below, call the appropriate tool to compute the value.
Work through them one by one.

Placeholders to compute:
{placeholder_list}
"""


def _detect_group_col(df: pd.DataFrame) -> str:
    for col in df.columns:
        if any(h in col.lower() for h in _GROUP_HINTS_RESOLVE):
            return col
    return ""


def _dispatch_tools(
    df: pd.DataFrame,
    placeholder_descriptions: dict[str, str],
    llm: ChatOpenAI,
) -> dict[str, str]:
    """Dispatch tool-calling LLM to compute all placeholder values from df."""
    if df.empty or not placeholder_descriptions:
        return {}

    group_col = _detect_group_col(df)
    sample_json = df.head(20).to_json(orient="records", indent=2)
    placeholder_list = "\n".join(
        f"  - {k}: {v}" for k, v in placeholder_descriptions.items()
    )

    messages = [
        SystemMessage(content=_RESOLVE_SYSTEM.format(
            group_col=group_col if group_col else "(none detected — omit group_by)",
        )),
        HumanMessage(content=_RESOLVE_USER.format(
            columns=list(df.columns),
            sample_rows_json=sample_json,
            placeholder_list=placeholder_list,
        )),
    ]

    try:
        response = llm.bind_tools(_RESOLVE_TOOLS).invoke(messages)
    except Exception as exc:
        logger.warning("[resolve] LLM dispatch failed: %s", exc)
        return {}

    full_data_json = df.to_json(orient="records")
    results: dict[str, str] = {}

    for tc in getattr(response, "tool_calls", []):
        name, args = tc["name"], {**tc["args"], "data_json": full_data_json}
        if name not in _RESOLVE_TOOL_MAP:
            continue
        try:
            value = _RESOLVE_TOOL_MAP[name].invoke(args)
        except Exception as exc:
            logger.warning("[resolve] Tool %s failed: %s", name, exc)
            continue
        col = args.get("column") or args.get("row_column") or ""
        col_l = col.lower()
        matched = next(
            (k for k, d in placeholder_descriptions.items()
             if col_l in d.lower() or k.lower() in col_l),
            next(iter(placeholder_descriptions), None),
        )
        if matched and matched not in results:
            results[matched] = value
            logger.info("[resolve] %s → %s", matched, str(value)[:80])

    return results


# ── GCS helpers ───────────────────────────────────────────────────────────────

_gcs: storage.Client | None = None


def _gcs_client() -> storage.Client:
    global _gcs
    if _gcs is None:
        _gcs = storage.Client()
    return _gcs


def _slug(s: str) -> str:
    return s.strip().lower().replace(" ", "_")


def _program_prefix(ta: str, dis: str, drug: str) -> str:
    return f"therapeutic-area/{_slug(ta)}/{_slug(dis)}/{_slug(drug)}"


def _load_manifest(bucket_name: str, prefix: str) -> dict | None:
    """Load manifest.json for a program, or return None if absent."""
    path = f"{prefix}/clinical_data/manifest.json"
    try:
        blob = _gcs_client().bucket(bucket_name).blob(path)
        if not blob.exists():
            return None
        return json.loads(blob.download_as_bytes())
    except Exception as exc:
        logger.warning("Could not load manifest %s: %s", path, exc)
        return None


def _load_csv(bucket_name: str, gcs_path: str) -> pd.DataFrame | None:
    """Stream a CSV from GCS into a pandas DataFrame."""
    try:
        data = _gcs_client().bucket(bucket_name).blob(gcs_path).download_as_bytes()
        return pd.read_csv(io.BytesIO(data))
    except Exception as exc:
        logger.warning("Could not load CSV at %s: %s", gcs_path, exc)
        return None


# ── Statistical analysis helpers ──────────────────────────────────────────────

def _compute_stats(df: pd.DataFrame) -> dict[str, Any]:
    """Return a compact statistical summary of a DataFrame."""
    stats: dict[str, Any] = {"n_rows": len(df), "n_cols": len(df.columns)}

    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            stats[col] = {
                "mean":   round(float(df[col].mean()), 3),
                "median": round(float(df[col].median()), 3),
                "std":    round(float(df[col].std()), 3),
                "min":    round(float(df[col].min()), 3),
                "max":    round(float(df[col].max()), 3),
                "missing": int(df[col].isna().sum()),
            }
        else:
            vc = df[col].value_counts(dropna=False).head(10)
            stats[col] = {
                "unique":          int(df[col].nunique()),
                "top_values":      {str(k): int(v) for k, v in vc.items()},
                "missing":         int(df[col].isna().sum()),
            }
    return stats


def _build_stats_summary(filename: str, df: pd.DataFrame, stats: dict) -> str:
    """Build a compact text representation of DataFrame statistics for the LLM."""
    lines = [f"### {filename}  (n={stats['n_rows']}, cols={stats['n_cols']})\n"]
    for col, s in stats.items():
        if col in ("n_rows", "n_cols"):
            continue
        if "mean" in s:
            lines.append(
                f"- **{col}**: mean={s['mean']}, median={s['median']}, "
                f"std={s['std']}, range=[{s['min']}, {s['max']}], missing={s['missing']}"
            )
        else:
            top = ", ".join(f"{k}={v}" for k, v in list(s["top_values"].items())[:5])
            lines.append(
                f"- **{col}**: {s['unique']} unique — [{top}] — missing={s['missing']}"
            )
    return "\n".join(lines)


# ── LLM helpers ───────────────────────────────────────────────────────────────

_ANALYST_SYSTEM = """\
You are a senior regulatory data scientist specialising in clinical drug development
and ICH M4E CTD submissions.

You will be given statistical summaries of clinical trial datasets.
Provide a structured, rigorous evidence synthesis covering:
  1. **Study population** — N, demographics (age, sex), baseline severity
  2. **Primary endpoint** — response rate or primary outcome at key timepoints
  3. **Secondary endpoints** — any additional efficacy parameters
  4. **Safety profile** — AE rates, SAEs, discontinuations
  5. **Clinical significance** — benefit–risk interpretation, NNT if computable

Format your response in Markdown.  Be precise — cite numbers from the data.
Flag any data quality issues or limitations observed.
"""

_QUERY_SYSTEM = """\
You are a senior regulatory data scientist specialising in clinical drug development.
Answer the user's question using ONLY the clinical data summaries provided.
Be concise, precise, and cite specific numbers.
Format your response in Markdown.
"""


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=_LLM_MODEL,
        temperature=0.0,
        api_key=os.environ.get("OPENAI_API_KEY"),
    )


# ── Core analysis function ────────────────────────────────────────────────────

def _run_analysis(
    ta: str,
    dis: str,
    drug: str,
    bucket_name: str,
    question: str | None = None,
    query_mode: bool = False,
) -> AnalysisResult:
    prefix   = _program_prefix(ta, dis, drug)
    manifest = _load_manifest(bucket_name, prefix)

    if not manifest:
        return AnalysisResult(
            reply=(
                f"⚠️ No clinical data found for `{ta} / {dis} / {drug}`.\n\n"
                "Upload a clinical CSV first using the **upload** button or say "
                "*upload clinical data for this program*."
            )
        )

    sources = manifest.get("sources") or []
    if not sources:
        return AnalysisResult(
            reply=(
                f"⚠️ The manifest for `{ta} / {dis} / {drug}` contains no sources.\n\n"
                "Re-upload the clinical CSV to regenerate the manifest."
            )
        )

    all_stats: dict[str, Any] = {}
    stats_texts: list[str]    = []

    for source in sources:
        gcs_path = source.get("gcs_path", "")
        filename = source.get("filename", gcs_path.rsplit("/", 1)[-1])

        df = _load_csv(bucket_name, gcs_path)
        if df is None:
            logger.warning("Skipping missing CSV: %s", gcs_path)
            continue

        # Truncate for safety
        if len(df) > _MAX_ROWS:
            df = df.sample(_MAX_ROWS, random_state=42)

        s = _compute_stats(df)
        all_stats[filename] = s
        stats_texts.append(_build_stats_summary(filename, df, s))

    if not stats_texts:
        return AnalysisResult(
            reply=(
                f"⚠️ Could not load any CSV files for `{ta} / {dis} / {drug}`. "
                "Check that the files exist in GCS."
            ),
            stats={},
        )

    combined_stats = "\n\n".join(stats_texts)
    system_prompt  = _QUERY_SYSTEM if query_mode else _ANALYST_SYSTEM
    user_content   = (
        f"Program: **{ta} / {dis} / {drug}**\n\n"
        f"{combined_stats}\n\n"
        + (f"User question: {question}" if question else
           "Provide a complete clinical evidence synthesis for this program.")
    )

    try:
        response = _llm().invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ])
        narrative = str(response.content)
    except Exception as exc:
        logger.error("LLM analysis failed: %s", exc)
        narrative = (
            f"⚠️ LLM analysis failed: {exc}\n\n"
            f"Raw statistics are available — {len(all_stats)} source(s) loaded."
        )

    return AnalysisResult(
        reply=narrative,
        stats=all_stats,
        state_patch={
            "content_program": {"ta": ta, "dis": dis, "drug": drug},
        },
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/resolve", response_model=ResolveResponse)
def resolve(req: ResolveRequest) -> ResolveResponse:
    """Pre-compute all {{placeholder}} values for a program before writing.

    Reads the clinical manifest, dispatches LLM tool-calling once per CSV
    source, and saves the resolved values to GCS as
    ``{program_prefix}/analysis/placeholder_values.json``.

    The content_worker calls this between the template step and every writer
    pass so that all three passes (Module 5, 2.7, 2.5) use the same numbers.
    """
    bucket = req.bucket or _DEFAULT_BUCKET
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket is required.")

    prefix   = _program_prefix(req.therapeutic_area, req.disease_type, req.drug_name)
    manifest = _load_manifest(bucket, prefix)

    if not manifest:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No clinical manifest found for "
                f"{req.therapeutic_area}/{req.disease_type}/{req.drug_name}. "
                "Upload clinical data first."
            ),
        )

    filter_keys: set[str] | None = set(req.placeholder_keys) if req.placeholder_keys else None

    # Build placeholder metadata from manifest column_mappings
    placeholder_meta: dict[str, dict] = {}
    for source in manifest.get("sources", []):
        gcs_path = source.get("gcs_path", "")
        for mapping in source.get("column_mappings", []):
            pk = mapping.get("placeholder_key", "")
            cn = mapping.get("column_name", "")
            if pk and cn and (filter_keys is None or pk in filter_keys):
                placeholder_meta[pk] = {
                    "gcs_path":    gcs_path,
                    "description": f"{mapping.get('role', '')} — column: {cn}",
                }

    saved_path = f"{prefix}/analysis/placeholder_values.json"

    if not placeholder_meta:
        _gcs_client().bucket(bucket).blob(saved_path).upload_from_string(
            json.dumps({}), content_type="application/json"
        )
        return ResolveResponse(
            resolved_values={}, saved_path=f"gs://{bucket}/{saved_path}",
            keys_resolved=0,
        )

    # Group by CSV source so each file is loaded only once
    file_groups: dict[str, dict[str, str]] = {}
    for pk, meta in placeholder_meta.items():
        file_groups.setdefault(meta["gcs_path"], {})[pk] = meta["description"]

    llm_client = _llm()
    resolved: dict[str, str] = {}

    for gcs_path, ph_descriptions in file_groups.items():
        df = _load_csv(bucket, gcs_path)
        if df is None or df.empty:
            logger.warning("[resolve] Skipping empty/missing CSV: %s", gcs_path)
            continue
        computed = _dispatch_tools(df, ph_descriptions, llm_client)
        resolved.update(computed)
        logger.info(
            "[resolve] %s: resolved %d/%d keys",
            gcs_path, len(computed), len(ph_descriptions),
        )

    keys_failed = [pk for pk in placeholder_meta if pk not in resolved]
    if keys_failed:
        logger.warning("[resolve] %d keys unresolved: %s", len(keys_failed), keys_failed)

    try:
        _gcs_client().bucket(bucket).blob(saved_path).upload_from_string(
            json.dumps(resolved, indent=2), content_type="application/json"
        )
        logger.info("[resolve] Saved %d values to gs://%s/%s", len(resolved), bucket, saved_path)
    except Exception as exc:
        logger.error("[resolve] GCS save failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to save resolved values: {exc}") from exc

    return ResolveResponse(
        resolved_values=resolved,
        saved_path=f"gs://{bucket}/{saved_path}",
        keys_resolved=len(resolved),
        keys_failed=keys_failed,
    )


@app.post("/analyze", response_model=AnalysisResult)
def analyze(req: AnalyzeRequest) -> AnalysisResult:
    """Full structured analysis — demographics, efficacy, safety, benefit-risk."""
    bucket = req.bucket or _DEFAULT_BUCKET
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket is required.")
    return _run_analysis(
        ta=req.therapeutic_area,
        dis=req.disease_type,
        drug=req.drug_name,
        bucket_name=bucket,
        question=req.question,
        query_mode=False,
    )


@app.post("/query", response_model=AnalysisResult)
def query(req: QueryRequest) -> AnalysisResult:
    """Free-form natural language Q&A against the clinical data."""
    bucket = req.bucket or _DEFAULT_BUCKET
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket is required.")
    return _run_analysis(
        ta=req.therapeutic_area,
        dis=req.disease_type,
        drug=req.drug_name,
        bucket_name=bucket,
        question=req.question,
        query_mode=True,
    )


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8084"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
