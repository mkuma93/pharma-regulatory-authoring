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
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from google.cloud import storage
from langchain_core.messages import HumanMessage, SystemMessage
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
