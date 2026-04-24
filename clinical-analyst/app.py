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

import ast
import io
import json
import logging
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from google.cloud import pubsub_v1, storage
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

try:
    from bq_audit import emit_function_registered, emit_placeholder_resolved  # noqa: F401
except ImportError:
    def emit_function_registered(**_): pass   # noqa: E704
    def emit_placeholder_resolved(**_): pass  # noqa: E704

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_BUCKET         = os.environ.get("GCS_BUCKET", "pharma-reguatory-author-life-science")
_LLM_MODEL              = os.environ.get("LLM_MODEL", "gpt-4o-mini")
_MAX_ROWS               = int(os.environ.get("MAX_ANALYSIS_ROWS", "1000"))
_GCP_PROJECT            = os.environ.get("GCP_PROJECT_ID", "pharma-reguatory-author")
_CONTENT_PUBSUB_TOPIC   = os.environ.get("CONTENT_PUBSUB_TOPIC", "ich4-content-generation")
_CONTENT_STATUS_TIMEOUT = int(os.environ.get("CONTENT_STATUS_TIMEOUT_SECONDS", str(30 * 60)))


def _load_secret(project: str, secret_name: str) -> str | None:
    """Fetch the latest version of a secret from GCP Secret Manager."""
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name   = f"projects/{project}/secrets/{secret_name}/versions/latest"
        resp   = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("utf-8").strip()
    except Exception as exc:
        logger.warning("[startup] Could not fetch secret %s: %s", secret_name, exc)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _GCP_PROJECT:
        logger.info("[startup] GCP_PROJECT_ID=%s — loading secrets from Secret Manager.", _GCP_PROJECT)
        openai_key = os.environ.get("OPENAI_API_KEY") or _load_secret(_GCP_PROJECT, "OPENAI_API_KEY")
        if not openai_key:
            raise RuntimeError("[startup] OPENAI_API_KEY not found in env or Secret Manager.")
        os.environ["OPENAI_API_KEY"] = openai_key
        logger.info("[startup] OPENAI_API_KEY present.")
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("[startup] OPENAI_API_KEY is required but not set.")
        logger.info("[startup] Using local env for secrets.")
    # Load persisted extension functions from GCS into _COMPUTATION_MAP
    if _DEFAULT_BUCKET:
        _load_extensions(_DEFAULT_BUCKET)
    yield


# ── FastAPI app ────────────────────────────────────────────────────────────

app = FastAPI(
    title="Clinical Analyst Service",
    version="1.0.0",
    description="Clinical data analysis and Q&A for regulatory authoring.",
    lifespan=lifespan,
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
    author: str = Field(default="", description="IAP user who triggered this run.")
    run_id: str = Field(default="", description="Content-generation run identifier.")


class ResolveResponse(BaseModel):
    resolved_values: dict[str, str]
    saved_path: str
    keys_resolved: int
    keys_failed: list[str] = Field(default_factory=list)
    keys_suggested: dict[str, str] = Field(
        default_factory=dict,
        description="Keys where a new computation type was needed. "
                    "Value is the suggestion or rejection reason.",
    )


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


# ── Planner / Executor ───────────────────────────────────────────────────────
#
# Architecture: LLM outputs structured intent only (closed vocabulary).
#               Deterministic executor owns all pandas logic — no code from LLM.
#               Range validator catches semantic errors post-execution.
#
# Computation vocabulary (the ONLY things the LLM may request):
#   proportion    – categorical column, proportion of rows matching a positive value
#   mean_sd       – numeric column, mean ± SD
#   median_range  – numeric column, median [min–max]
#   row_count     – total row count, no column needed
#   unique_count  – distinct non-null values in a column

_NARRATIVE_SUFFIXES = (
    "_overview", "_conclusions", "_rationale", "_summary",
    "_description", "_profile", "_narrative",
    "_endpoint", "_timepoint", "_indication", "_wording",
    "_conclusion", "_population", "_criteria", "_label",
)

# Keys whose prefix pattern signals a multi-group derived stat (NNT, ARR, HR, OR …).
# These are forced to "suggest" even if the planner tries a simple builtin.
_DERIVED_STAT_PREFIXES = ("nnt_", "arr_", "rrr_", "hratio_", "oratio_", "hr_",
                          "or_", "rr_", "ci_", "km_")

# ── Computation registry ──────────────────────────────────────────────────────
# All executor functions share signature: (df: pd.DataFrame, step: dict) -> str
# "step" is the planner JSON object with keys: computation, column, positive_value, etc.

def _exec_proportion(df: pd.DataFrame, step: dict) -> str:
    col, pos = step["column"], step["positive_value"]
    pct = df[col].eq(pos).mean() * 100
    n   = int(df[col].eq(pos).sum())
    return f"{pct:.1f}% ({n}/{len(df)})"

def _exec_mean_sd(df: pd.DataFrame, step: dict) -> str:
    s = pd.to_numeric(df[step["column"]], errors="coerce")
    valid_n = int(s.notna().sum())
    if valid_n == 0:
        raise ValueError(f"column '{step['column']}' has no numeric values")
    return f"{s.mean():.1f} \u00b1 {s.std():.1f} (n={valid_n})"

def _exec_median_range(df: pd.DataFrame, step: dict) -> str:
    s = pd.to_numeric(df[step["column"]], errors="coerce")
    valid_n = int(s.notna().sum())
    if valid_n == 0:
        raise ValueError(f"column '{step['column']}' has no numeric values")
    return (f"{s.median():.1f} [{s.min():.0f}\u2013{s.max():.0f}]"
            f" (n={valid_n})")

def _exec_row_count(df: pd.DataFrame, step: dict) -> str:
    return str(len(df))

def _exec_unique_count(df: pd.DataFrame, step: dict) -> str:
    return str(df[step["column"]].nunique())

# Mutable registry — extended at startup + at runtime by generated functions
_COMPUTATION_MAP: dict[str, Callable] = {
    "proportion":   _exec_proportion,
    "mean_sd":      _exec_mean_sd,
    "median_range": _exec_median_range,
    "row_count":    _exec_row_count,
    "unique_count": _exec_unique_count,
}
_COMPUTATION_MAP_LOCK = threading.Lock()

# Built-in types that cannot be overwritten by generated functions
_BUILTIN_COMPUTATIONS = frozenset(_COMPUTATION_MAP.keys())

# "suggest" is not executable — it signals a missing computation type
_ALLOWED_COMPUTATIONS = _BUILTIN_COMPUTATIONS | {"suggest"}

_GROUP_HINTS_RESOLVE = [
    "treatment", "arm", "group", "intervention", "randomiz", "cohort", "stratum"
]

# ── Range rules ───────────────────────────────────────────────────────────────
_RANGE_RULES: dict[str, tuple[float, float]] = {
    "proportion":   (0.0,  100.0),
    "mean_sd":      (-1e9,  1e9),
    "median_range": (-1e9,  1e9),
    "row_count":    (1.0,   1e7),
    "unique_count": (1.0,   1e5),
}

# ── Extension module (persisted to GCS) ──────────────────────────────────────
_EXTENSION_GCS_PATH  = "system/computed_extensions.py"
_EXT_MODULE_LOCK     = threading.Lock()
_EXT_MODULE_HEADER   = '''\
"""Auto-generated clinical computation extensions.
Generated by clinical-analyst service. Do not edit manually.
Each function signature: (df: pd.DataFrame, step: dict) -> str
"""
import re
import numpy as np
import pandas as pd

_REGISTRY: dict = {}
'''
_AST_ALLOWED_ATTRS = {
    # pandas Series / DataFrame methods
    "eq", "ne", "gt", "lt", "ge", "le", "mean", "sum", "std", "min", "max",
    "median", "count", "nunique", "value_counts", "groupby", "apply",
    "dropna", "notna", "isna", "fillna", "astype", "str", "dt",
    "to_numeric", "reset_index", "rename", "head", "tail", "copy",
    # numpy
    "sqrt", "log", "exp", "abs", "round", "nan", "inf",
    # builtins kept in safe eval context
    "len", "int", "float", "str", "round", "range", "enumerate",
}


def _ast_safe(code: str) -> str | None:
    """Return error message if code is structurally unsafe, else None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Only pandas / numpy / re allowed
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for nm in names:
                if nm and nm.split(".")[0] not in ("pandas", "pd", "numpy", "np", "re"):
                    return f"Forbidden import: {nm}"
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                return f"Dunder attribute access forbidden: {node.attr}"
    return None


def _load_extensions(bucket: str) -> None:
    """Load and exec the extension module from GCS, registering functions."""
    try:
        blob = _gcs_client().bucket(bucket).blob(_EXTENSION_GCS_PATH)
        if not blob.exists():
            logger.info("[ext] No extension module found at %s — starting fresh", _EXTENSION_GCS_PATH)
            return
        code = blob.download_as_text()
        ns: dict = {"pd": pd}
        exec(compile(code, _EXTENSION_GCS_PATH, "exec"), ns)  # noqa: S102
        registry: dict = ns.get("_REGISTRY", {})
        with _COMPUTATION_MAP_LOCK:
            _COMPUTATION_MAP.update(registry)
        logger.info("[ext] Loaded %d extension function(s): %s",
                    len(registry), list(registry.keys()))
    except Exception as exc:
        logger.warning("[ext] Failed to load extension module: %s", exc)


_FUNCTION_MANIFEST_PATH = "system/function_manifest.json"


def _update_function_manifest(
    bucket: str, comp_type: str, fn_name: str,
    author: str = "", run_id: str = "",
) -> None:
    """Upsert an entry in the function manifest (comp_type → metadata)."""
    try:
        mblob   = _gcs_client().bucket(bucket).blob(_FUNCTION_MANIFEST_PATH)
        manifest: dict[str, dict] = (
            json.loads(mblob.download_as_text()) if mblob.exists() else {}
        )
        now = datetime.now(timezone.utc).isoformat()
        if comp_type not in manifest:
            # First time this comp_type is created
            manifest[comp_type] = {
                "fn_name":      fn_name,
                "created_at":   now,
                "created_by":   author or "system",
                "run_id":       run_id,
                "source":       "clinical-analyst/resolve",
                "version":      1,
            }
        else:
            # Subsequent regeneration: bump version, record who overwrote it
            manifest[comp_type]["version"]     = manifest[comp_type].get("version", 1) + 1
            manifest[comp_type]["updated_at"]  = now
            manifest[comp_type]["updated_by"]  = author or "system"
            manifest[comp_type]["fn_name"]      = fn_name
        mblob.upload_from_string(
            json.dumps(manifest, indent=2), content_type="application/json"
        )
        logger.info("[ext] function_manifest updated for '%s' (v%s)",
                    comp_type, manifest[comp_type]["version"])
    except Exception as exc:
        logger.warning("[ext] function_manifest update failed for '%s': %s", comp_type, exc)


def _persist_extension(
    fn_name: str, comp_type: str, fn_code: str, bucket: str,
    author: str = "", run_id: str = "",
    codegen_messages: list[dict] | None = None,
) -> None:
    """Append a new function to the GCS extension module and hot-register it."""
    with _EXT_MODULE_LOCK:
        try:
            blob = _gcs_client().bucket(bucket).blob(_EXTENSION_GCS_PATH)
            current = blob.download_as_text() if blob.exists() else _EXT_MODULE_HEADER
            entry = (
                f"\n# ── Generated {datetime.now(timezone.utc).date()} ─────────\n"
                f"{fn_code}\n"
                f'_REGISTRY["{comp_type}"] = {fn_name}\n'
            )
            updated = current + entry
            blob.upload_from_string(updated, content_type="text/plain")
            logger.info("[ext] Persisted extension '%s' to GCS", comp_type)
        except Exception as exc:
            logger.warning("[ext] GCS persist failed for '%s': %s", comp_type, exc)
            return

    _update_function_manifest(bucket, comp_type, fn_name, author=author, run_id=run_id)
    emit_function_registered(run_id=run_id, author=author, comp_type=comp_type, fn_name=fn_name)

    # Archive the codegen prompt alongside the function
    if codegen_messages:
        try:
            prompt_blob = _gcs_client().bucket(bucket).blob(
                f"system/prompts/{comp_type}_codegen.json"
            )
            prompt_blob.upload_from_string(
                json.dumps({
                    "comp_type":    comp_type,
                    "fn_name":      fn_name,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "author":       author or "system",
                    "run_id":       run_id,
                    "messages":     codegen_messages,
                }, indent=2),
                content_type="application/json",
            )
            logger.info("[ext] Saved codegen prompt for '%s'", comp_type)
        except Exception as exc:
            logger.warning("[ext] Codegen prompt save failed for '%s': %s", comp_type, exc)

    # Hot-register in memory
    ns: dict = {"pd": pd}
    try:
        exec(compile(fn_code, "<generated>", "exec"), ns)  # noqa: S102
        fn = ns.get(fn_name)
        if fn:
            with _COMPUTATION_MAP_LOCK:
                _COMPUTATION_MAP[comp_type] = fn
            logger.info("[ext] Hot-registered '%s'", comp_type)
    except Exception as exc:
        logger.warning("[ext] Hot-register failed for '%s': %s", comp_type, exc)


# ── Code generator + validator ────────────────────────────────────────────────

_CODEGEN_SYSTEM = """\
You are an expert clinical data scientist writing a Python function.

Write ONE Python function with this exact signature:
  def {fn_name}(df: pd.DataFrame, step: dict) -> str:

Rules:
- Use ONLY pandas (pd) and numpy (np) — no other imports inside the function.
- The function receives:
    df   — a pandas DataFrame with the columns listed below
    step — a dict with at least: "key", "computation", "column"
           (and any extra fields the caller passes, e.g. "time_column", "event_column")
- Return a concise human-readable string (e.g. "12.3 months [2–48]").
- Do NOT print, log, or raise exceptions — return an error string instead.
- Do NOT read files, make network calls, or access global state.
- Include a one-line docstring describing what is computed.
- Return ONLY the function definition — no explanation, no markdown fences.
"""

_CODEGEN_USER = """\
Computation needed: {suggestion}

Dataset schema:
{schema_json}

The step dict will include: {step_fields}

Write the function named: {fn_name}
"""

_CODEVALIDATOR_SYSTEM = """\
You are an adversarial code reviewer for clinical statistics.
Review the Python function below.

Check for:
1. Statistical correctness — does the logic match the described computation?
2. Correct pandas/numpy API usage — no deprecated or wrong methods.
3. Edge cases handled — empty series, all-NaN, zero division.
4. No security issues — no exec, eval, open, import inside body, __dunder__ access.
5. Return type — must return a string in all code paths.
6. Not redundant with builtins — if the code is merely computing a simple proportion,
   mean, median, or count of a single column, reject it and explain which builtin to use
   instead (proportion / mean_sd / median_range / row_count / unique_count).

Respond with ONLY a JSON object:
  {{"approved": true, "reason": "brief positive note"}}
  {{"approved": false, "reason": "specific problem found"}}
"""


def _derive_fn_name(comp_type: str) -> str:
    safe = re.sub(r"[^a-z0-9_]", "_", comp_type.lower())
    return f"_ext_{safe}"


def _generate_extension_code(
    suggestion: str,
    step: dict,
    schema: list[dict],
    llm: ChatOpenAI,
) -> tuple[str | None, list[dict]]:
    """Ask LLM to write a new executor function.

    Returns (source_code_or_None, serialised_messages) so callers can
    archive the prompt that produced the function.
    """
    comp_type = step.get("computation", "")
    fn_name   = _derive_fn_name(comp_type)
    step_fields = ", ".join(f'"{k}"' for k in step if k != "data_json")

    messages = [
        SystemMessage(content=_CODEGEN_SYSTEM.format(fn_name=fn_name)),
        HumanMessage(content=_CODEGEN_USER.format(
            suggestion=suggestion,
            schema_json=json.dumps(schema, indent=2),
            step_fields=step_fields,
            fn_name=fn_name,
        )),
    ]
    serialised = [{"role": m.type, "content": m.content} for m in messages]

    try:
        response = llm.invoke(messages)
        return str(response.content).strip(), serialised
    except Exception as exc:
        logger.warning("[ext] Code generation failed: %s", exc)
        return None, serialised


def _validate_extension_code(code: str, llm: ChatOpenAI) -> tuple[bool, str]:
    """Have a second LLM instance review the generated code. Returns (approved, reason)."""
    # AST structural check first — fast and free
    ast_err = _ast_safe(code)
    if ast_err:
        return False, f"AST check failed: {ast_err}"

    try:
        response = llm.invoke([
            SystemMessage(content=_CODEVALIDATOR_SYSTEM),
            HumanMessage(content=code),
        ])
        raw = str(response.content).strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
        verdict = json.loads(raw)
        return bool(verdict.get("approved")), str(verdict.get("reason", ""))
    except Exception as exc:
        return False, f"validator error: {exc}"


# ── Planner prompt ────────────────────────────────────────────────────────────

_PLANNER_SYSTEM = """\
You are a clinical data analyst planner. Your ONLY job is to produce a JSON array
describing HOW to compute each placeholder value from the dataset schema.

Allowed computation types (use ONLY these exact strings):
  "proportion"   – fraction of rows where a categorical column equals a specific value.
                   Requires: column (string), positive_value (exact string from sample_values).
  "mean_sd"      – mean ± standard deviation of a numeric column.
                   Requires: column (numeric).
  "median_range" – median [min–max] of a numeric column.
                   Requires: column (numeric).
  "row_count"    – total number of rows. No column needed.
  "unique_count" – count of distinct non-null values in a column.
                   Requires: column (string or categorical).
  "suggest"      – LAST RESORT ONLY. Use when none of the above types can produce the
                   result (e.g. NNT, hazard ratio, conditional proportion, regression).
                   Requires: column (best candidate), suggestion (one sentence describing
                   exactly what statistical computation is needed and why).

Output format — a JSON array, one object per computable placeholder:
[
  {{"key": "<placeholder_key>", "computation": "<type>", "column": "<exact_column_name>",
    "positive_value": "<exact_value_from_sample_values>"}},
  {{"key": "<other_key>", "computation": "suggest", "column": "<column>",
    "suggestion": "<description of needed computation>"}}
]

BUILTIN PREFERENCE RULES (ALWAYS prefer a builtin before using "suggest"):
- Key ends in _percent, _rate, _proportion, or means "recovery", "response", "incidence"
  → use "proportion" (pick the right column and positive_value from sample_values).
- Key is a score, age, weight, or other continuous numeric measure
  → use "mean_sd" or "median_range" (examine column dtype — numeric → mean_sd).
- Key is a count of subjects or events
  → use "row_count" (no column needed).
- Key is "number of X" where X is a categorical treatment/group
  → use "unique_count".
- "suggest" is ONLY valid when the computation genuinely requires arithmetic across
  multiple columns, conditional filtering by group, or a derived statistic not
  expressible as a single column summary (e.g. NNT, ARR, odds ratio, Kaplan-Meier).

Additional rules:
- Use ONLY column names from the schema provided. Never invent column names.
- For "proportion": positive_value MUST be copied verbatim from the sample_values list.
- For "row_count": omit "column" and "positive_value" fields entirely.
- Skip placeholders describing free-text narratives (ends in _overview, _conclusions,
  _rationale, _summary, _description, _profile, _narrative, _endpoint, _timepoint,
  _indication, _wording, _conclusion, _population, _criteria, _label).
- If you cannot map a placeholder to any builtin, use "suggest" — never skip silently.
- Return ONLY the JSON array, no explanation, no markdown fences.
"""

_PLANNER_USER = """\
Dataset schema:
{schema_json}

Placeholders to plan (key → description):
{placeholder_list}
"""


def _build_schema(df: pd.DataFrame) -> list[dict]:
    """Build a column schema with dtype and up to 5 sample distinct values."""
    schema = []
    for col in df.columns:
        dtype = str(df[col].dtype)
        if df[col].dtype == object:
            sample_vals = df[col].dropna().unique()[:5].tolist()
            kind = "categorical"
        else:
            sample_vals = []
            kind = "numeric"
        schema.append({"column": col, "dtype": dtype, "kind": kind,
                        "sample_values": sample_vals})
    return schema


def _validate_plan_step(step: dict, df_columns: set[str]) -> str | None:
    """Return an error string if the step is structurally invalid, else None."""
    computation = step.get("computation", "")
    column      = step.get("column", "")
    if computation not in _ALLOWED_COMPUTATIONS:
        return f"unknown computation '{computation}'"
    if computation == "suggest":
        return None
    if computation != "row_count":
        if not column:
            return "missing column"
        if column not in df_columns:
            return f"column '{column}' not in dataset"
    if computation == "proportion" and not step.get("positive_value"):
        return "proportion requires positive_value"
    return None


def _execute_step(step: dict, df: pd.DataFrame) -> str:
    """Look up and call the registered executor for the computation type."""
    comp = step["computation"]
    fn = _COMPUTATION_MAP.get(comp)
    if fn is None:
        raise ValueError(f"no executor registered for '{comp}'")
    return fn(df, step)


def _validate_result(key: str, computation: str, value: str) -> bool:
    """Post-execution range check — returns False when value looks implausible."""
    rules = _RANGE_RULES.get(computation)
    if rules is None:
        return True
    lo, hi = rules
    m = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    if not m:
        return False
    num = float(m.group())
    if not (lo <= num <= hi):
        logger.warning("[resolve] %s range check failed: %s not in [%s, %s]",
                       key, num, lo, hi)
        return False
    return True


def _plan_and_execute(
    df: pd.DataFrame,
    placeholder_meta: dict[str, dict],
    llm: ChatOpenAI,
    bucket: str,
    author: str = "",
    run_id: str = "",
) -> tuple[dict[str, str], dict[str, str], dict[str, dict], list[dict]]:
    """LLM plans intent → executor runs it → range validator checks.

    Returns (resolved, suggested, provenance, planner_messages).
    provenance: {key: {comp_type, fn_name, source}} — records which function
    produced each resolved value so documents are fully traceable.
    planner_messages: serialised system+user messages sent to the planner LLM,
    for audit archiving alongside placeholder_provenance.json.
    """
    if df.empty or not placeholder_meta:
        return {}, {}, {}, []

    computable_meta = {
        k: v for k, v in placeholder_meta.items()
        if not any(k.lower().endswith(sfx) for sfx in _NARRATIVE_SUFFIXES)
    }
    if not computable_meta:
        return {}, {}, {}, []

    schema       = _build_schema(df)
    schema_json  = json.dumps(schema, indent=2)
    placeholder_list = "\n".join(
        f"  {k}: {v['description']}" for k, v in computable_meta.items()
    )

    planner_messages = [
        SystemMessage(content=_PLANNER_SYSTEM),
        HumanMessage(content=_PLANNER_USER.format(
            schema_json=schema_json, placeholder_list=placeholder_list,
        )),
    ]
    serialised_planner = [{"role": m.type, "content": m.content} for m in planner_messages]

    try:
        raw = str(llm.invoke(planner_messages).content).strip()
    except Exception as exc:
        logger.warning("[resolve] planner LLM failed: %s", exc)
        return {}, {}, {}, serialised_planner

    try:
        plan: list[dict] = json.loads(raw)
        if not isinstance(plan, list):
            raise ValueError("not a list")
    except Exception as exc:
        logger.warning("[resolve] bad planner JSON: %s | raw: %s", exc, raw[:200])
        return {}, {}, {}, serialised_planner

    df_columns = set(df.columns)
    results:   dict[str, str] = {}
    suggested: dict[str, str] = {}
    provenance: dict[str, dict] = {}  # key → {comp_type, fn_name, source}

    for step in plan:
        key = step.get("key", "")
        if not key or key not in computable_meta or key in results:
            continue

        err = _validate_plan_step(step, df_columns)
        if err:
            logger.warning("[resolve] step rejected '%s': %s", key, err)
            suggested[key] = f"[plan rejected] {err}"
            continue

        # ── derived-stat guard: override builtin with suggest for NNT/ARR/HR etc. ──
        if step["computation"] != "suggest" and any(
            key.lower().startswith(pfx) for pfx in _DERIVED_STAT_PREFIXES
        ):
            logger.info("[resolve] forcing suggest for derived stat key '%s'", key)
            step["computation"] = "suggest"
            if not step.get("suggestion"):
                step["suggestion"] = (
                    f"Compute {key} — a derived multi-group statistic "
                    f"(e.g. NNT/ARR/HR/OR) using the clinical trial columns."
                )

        # ── suggest path: generate → validate → persist → hot-register ────
        if step["computation"] == "suggest":
            suggestion = step.get("suggestion", "(no description)")
            comp_type  = re.sub(r"[^a-z0-9_]", "_", key.lower())

            if comp_type not in _COMPUTATION_MAP:
                code, codegen_msgs = _generate_extension_code(suggestion, step, schema, llm)
                if code:
                    approved, reason = _validate_extension_code(code, llm)
                    if approved:
                        _persist_extension(
                            _derive_fn_name(comp_type), comp_type, code, bucket,
                            author=author, run_id=run_id,
                            codegen_messages=codegen_msgs,
                        )
                    else:
                        logger.warning("[ext] Rejected '%s': %s", comp_type, reason)
                        # If validator says to use a builtin, reroute — but only for
                        # simple keys, never for derived multi-group stats.
                        is_derived = any(
                            key.lower().startswith(pfx) for pfx in _DERIVED_STAT_PREFIXES
                        )
                        rerouted = False
                        if not is_derived:
                            for builtin in _BUILTIN_COMPUTATIONS:
                                if builtin in reason.lower():
                                    logger.info(
                                        "[ext] Rerouting '%s' → builtin '%s'", key, builtin
                                    )
                                    step["computation"] = builtin
                                    rerouted = True
                                    break
                        if not rerouted:
                            suggested[key] = (
                                f"[needs new function] {suggestion} — rejected: {reason}"
                            )
                            continue
                else:
                    suggested[key] = f"[needs new function] {suggestion}"
                    continue

            if comp_type in _COMPUTATION_MAP:
                step["computation"] = comp_type
            elif step["computation"] not in _BUILTIN_COMPUTATIONS:
                suggested[key] = f"[needs new function] {suggestion}"
                continue

        # ── manifest positive_value wins for proportion ────────────────────
        if step.get("computation") == "proportion":
            pv = computable_meta[key].get("positive_value")
            if pv:
                step["positive_value"] = pv
            elif not step.get("positive_value"):
                # Infer positive_value from column sample_values: prefer "Yes" / "Female" /
                # "Male" / first sample; fall back to first categorical sample value.
                col = step.get("column", "")
                key_lower = key.lower()
                if col in df.columns:
                    samples = df[col].dropna().unique().tolist()
                    preferred = None
                    if "yes" in [str(s).lower() for s in samples]:
                        preferred = next(s for s in samples if str(s).lower() == "yes")
                    elif "female" in key_lower and any(str(s).lower() == "female" for s in samples):
                        preferred = next(s for s in samples if str(s).lower() == "female")
                    elif "male" in key_lower and any(str(s).lower() == "male" for s in samples):
                        preferred = next(s for s in samples if str(s).lower() == "male")
                    else:
                        preferred = samples[0] if samples else None
                    if preferred is not None:
                        step["positive_value"] = str(preferred)

        try:
            value = _execute_step(step, df)
        except Exception as exc:
            logger.warning("[resolve] execution failed '%s': %s", key, exc)
            suggested[key] = f"[execution error] {exc}"
            continue

        if not _validate_result(key, step["computation"], value):
            logger.warning("[resolve] range check failed '%s': %s", key, value)
            suggested[key] = f"[range check failed] computed: {value}"
            continue

        results[key] = value
        provenance[key] = {
            "comp_type": step["computation"],
            "fn_name":   _derive_fn_name(step["computation"]),
            "source":    "builtin" if step["computation"] in _BUILTIN_COMPUTATIONS else "generated",
        }
        logger.info("[resolve] %s → %s", key, value[:80])

    unresolved = [k for k in computable_meta if k not in results and k not in suggested]
    if unresolved:
        logger.warning("[resolve] %d unresolved: %s", len(unresolved), unresolved)

    return results, suggested, provenance, serialised_planner


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
    """Stream a CSV from GCS into a pandas DataFrame.

    Accepts either a full ``gs://bucket/path`` URI or a bucket-relative blob
    path.  The bucket-relative path is always derived by stripping the
    ``gs://<bucket>/`` prefix when present.
    """
    try:
        blob_path = gcs_path
        full_prefix = f"gs://{bucket_name}/"
        if blob_path.startswith(full_prefix):
            blob_path = blob_path[len(full_prefix):]
        elif blob_path.startswith("gs://"):
            # URI for a different bucket — extract bucket + path
            without = blob_path[len("gs://"):]
            _, blob_path = without.split("/", 1)
        data = _gcs_client().bucket(bucket_name).blob(blob_path).download_as_bytes()
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
            "content_program": {"therapeutic_area": ta, "disease_type": dis, "drug_name": drug},
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
    sources = manifest.get("sources", [])
    for source in sources:
        gcs_path = source.get("gcs_path", "")
        for mapping in source.get("column_mappings", []):
            pk = mapping.get("placeholder_key", "")
            cn = mapping.get("column_name", "")
            if pk and cn and (filter_keys is None or pk in filter_keys):
                placeholder_meta[pk] = {
                    "gcs_path":      gcs_path,
                    "description":   f"{mapping.get('role', '')} — column: {cn}",
                    "positive_value": mapping.get("positive_value"),  # from manifest
                }

    # Schema-only inference: for requested keys absent from manifest, let the
    # planner try to derive them from the CSV schema (computation="suggest" likely).
    if filter_keys:
        first_gcs_path = next(
            (s.get("gcs_path", "") for s in sources if s.get("gcs_path")), ""
        )
        for pk in filter_keys:
            if pk not in placeholder_meta and first_gcs_path:
                placeholder_meta[pk] = {
                    "gcs_path":      first_gcs_path,
                    "description":   f"Inferred — no manifest entry; derive from CSV schema",
                    "positive_value": None,
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
    file_groups: dict[str, dict[str, dict]] = {}
    for pk, meta in placeholder_meta.items():
        file_groups.setdefault(meta["gcs_path"], {})[pk] = meta

    llm_client    = _llm()
    resolved:      dict[str, str]   = {}
    all_suggested: dict[str, str]   = {}
    all_provenance: dict[str, dict] = {}
    all_planner_prompts: list[dict] = []  # one entry per CSV group

    for gcs_path, ph_meta in file_groups.items():
        df = _load_csv(bucket, gcs_path)
        if df is None or df.empty:
            logger.warning("[resolve] Skipping empty/missing CSV: %s", gcs_path)
            continue
        comp, sugg, prov, planner_msgs = _plan_and_execute(
            df, ph_meta, llm_client, bucket,
            author=req.author, run_id=req.run_id,
        )
        resolved.update(comp)
        all_suggested.update(sugg)
        all_provenance.update(prov)
        for key, prov_info in prov.items():
            emit_placeholder_resolved(
                run_id=req.run_id,
                author=req.author,
                therapeutic_area=req.therapeutic_area,
                disease_type=req.disease_type,
                drug_name=req.drug_name,
                placeholder_key=key,
                comp_type=prov_info.get("comp_type", ""),
                fn_name=prov_info.get("fn_name", ""),
                csv_source=gcs_path,
                resolved_value=str(comp.get(key, "")),
            )
        if planner_msgs:
            all_planner_prompts.append({
                "csv_source": gcs_path,
                "messages":   planner_msgs,
            })
        logger.info(
            "[resolve] %s: resolved %d/%d keys, %d suggested",
            gcs_path, len(comp), len(ph_meta), len(sugg),
        )

    keys_failed = [pk for pk in placeholder_meta
                   if pk not in resolved and pk not in all_suggested]
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

    # Save provenance sidecar — records which function produced each value
    provenance_path = f"{prefix}/analysis/placeholder_provenance.json"
    try:
        now = datetime.now(timezone.utc).isoformat()
        provenance_doc = {
            "run_id":    req.run_id,
            "author":    req.author or "system",
            "resolved_at": now,
            "values": {
                k: {
                    "value":     resolved.get(k, ""),
                    "comp_type": v.get("comp_type", ""),
                    "fn_name":   v.get("fn_name", ""),
                    "source":    v.get("source", ""),
                }
                for k, v in all_provenance.items()
            },
        }
        _gcs_client().bucket(bucket).blob(provenance_path).upload_from_string(
            json.dumps(provenance_doc, indent=2), content_type="application/json"
        )
        logger.info("[resolve] Saved provenance (%d keys) to gs://%s/%s",
                    len(all_provenance), bucket, provenance_path)
    except Exception as exc:
        logger.warning("[resolve] Provenance save failed (non-fatal): %s", exc)

    # Save the planner prompts used for this resolve run (one per CSV group)
    if all_planner_prompts:
        planner_prompt_path = f"{prefix}/analysis/planner_prompts.json"
        try:
            _gcs_client().bucket(bucket).blob(planner_prompt_path).upload_from_string(
                json.dumps({
                    "run_id":     req.run_id,
                    "author":     req.author or "system",
                    "saved_at":   datetime.now(timezone.utc).isoformat(),
                    "groups":     all_planner_prompts,
                }, indent=2),
                content_type="application/json",
            )
            logger.info("[resolve] Saved planner prompts to gs://%s/%s", bucket, planner_prompt_path)
        except Exception as exc:
            logger.warning("[resolve] Planner prompt save failed (non-fatal): %s", exc)

    return ResolveResponse(
        resolved_values=resolved,
        saved_path=f"gs://{bucket}/{saved_path}",
        keys_resolved=len(resolved),
        keys_failed=keys_failed,
        keys_suggested=all_suggested,
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


# ── Content generation trigger + status ──────────────────────────────────────
#
# These endpoints were previously part of ctd-structure/api (out of scope there).
# Clinical-analyst owns:
#   • Checking whether clinical data is present before generating content
#   • Publishing the content-generation job to Pub/Sub
#   • Reading back the content-generation status written by content_worker


class TriggerRequest(BaseModel):
    session_id: str
    bucket: str | None = Field(default=None)
    therapeutic_area: str
    disease_type: str
    drug_name: str
    author: str = Field(
        default="",
        description="IAP-authenticated user email — propagated to version manifest.",
    )
    force_no_clinical: bool = Field(
        default=False,
        description="Proceed even when no clinical manifest is found.",
    )
    state: dict = Field(default_factory=dict)


class ActionResponse(BaseModel):
    """Standard reply + state-patch shape used across all services."""
    reply: str
    state_patch: dict = Field(default_factory=dict)


def _publish_content_generation(
    bucket: str, ta: str, dis: str, drug: str, session_id: str, author: str = ""
) -> str:
    run_id    = str(uuid.uuid4())
    publisher = pubsub_v1.PublisherClient()
    topic     = publisher.topic_path(_GCP_PROJECT, _CONTENT_PUBSUB_TOPIC)
    payload   = json.dumps({
        "bucket":     bucket,
        "session_id": session_id,
        "run_id":     run_id,
        "author":     author,
        "program": {
            "therapeutic_area": ta,
            "disease_type":     dis,
            "drug_name":        drug,
        },
    }).encode()
    publisher.publish(topic, payload).result(timeout=10)
    return run_id


def _summarise_clinical_manifest(manifest: dict) -> str:
    sources = manifest.get("sources", [])
    if not sources:
        return "_(manifest found but no datasets registered)_"
    lines = [f"📋 **{len(sources)} clinical dataset(s) registered:**\n"]
    for src in sources:
        cols    = src.get("column_mappings", [])
        secs    = src.get("ctd_section_keys", [])
        sec_str = ", ".join(sorted(secs)[:5]) or "—"
        lines.append(
            f"- **{src.get('filename', '?')}** — {src.get('study_type', '?')} study "
            f"({len(cols)} mapped columns → CTD sections: {sec_str})"
        )
        endpoints = [f"`{m['placeholder_key']}`" for m in cols[:6]]
        if endpoints:
            more = f" + {len(cols) - 6} more" if len(cols) > 6 else ""
            lines.append(f"  Endpoints mapped: {', '.join(endpoints)}{more}")
    return "\n".join(lines)


def _load_content_status(bucket: str, ta: str, dis: str, drug: str) -> dict:
    prefix  = f"{_program_prefix(ta, dis, drug)}/content_status/"
    bkt_obj = _gcs_client().bucket(bucket)
    try:
        raw = bkt_obj.blob(f"{prefix}latest.json").download_as_text()
        job = json.loads(raw)
    except Exception:
        job = None
    if job is None:
        try:
            blobs = sorted(
                bkt_obj.list_blobs(prefix=prefix),
                key=lambda b: b.updated or b.time_deleted,
                reverse=True,
            )
            for blob in blobs:
                if blob.name.endswith(".json"):
                    job = json.loads(blob.download_as_text())
                    break
        except Exception:
            pass
    if job is None:
        return {}
    if job.get("status") == "running":
        updated_at = job.get("updated_at")
        if updated_at:
            try:
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(updated_at)).total_seconds()
                if age > _CONTENT_STATUS_TIMEOUT:
                    return {"status": "timed_out", "age_minutes": round(age / 60)}
            except Exception:
                pass
    return job


@app.post("/trigger", response_model=ActionResponse)
def trigger(req: TriggerRequest) -> ActionResponse:
    """Trigger CTD content generation for a drug program.

    1. If ``force_no_clinical`` is False, verifies clinical data is loaded.
       Returns a confirmation prompt when data is missing.
    2. Publishes a message to the ``ich4-content-generation`` Pub/Sub topic.
    3. Returns reply text + state_patch for the chat UI.

    Moved here from ``ctd-structure/api`` where it was out of scope.
    """
    bucket = (req.bucket or _DEFAULT_BUCKET).strip()
    if not bucket:
        raise HTTPException(status_code=422, detail="bucket is required.")

    ta   = req.therapeutic_area.strip()
    dis  = req.disease_type.strip()
    drug = req.drug_name.strip()

    # ── Prerequisite: program CTD folder must exist in GCS ───────────────────────
    # The scaffold step (ctd-api /copy) creates {prefix}/ctd/.keep blobs. If none
    # exist, the CTD structure has not been copied for this program yet.
    prefix = _program_prefix(ta, dis, drug)
    ctd_prefix = f"{prefix}/ctd/"
    ctd_blobs = list(_gcs_client().bucket(bucket).list_blobs(prefix=ctd_prefix, max_results=1))
    if not ctd_blobs:
        return ActionResponse(
            reply=(
                f"⚠️ **Program folder not scaffolded** for **{ta} / {dis} / {drug}**.\n"
                "Content generation requires the CTD folder structure to be set up first.\n\n"
                "**Steps to fix:**\n"
                "1. Make sure the default ICH CTD structure has been **built and approved**\n"
                "2. Say **set up authoring for "
                f"`{ta} / {dis} / {drug}`** to scaffold the program folder\n"
                "3. Then say **generate content** again."
            ),
            state_patch={},
        )

    if not req.force_no_clinical:
        manifest = _load_manifest(bucket, _program_prefix(ta, dis, drug))
        if not manifest or not manifest.get("sources"):
            return ActionResponse(
                reply=(
                    f"⚠️ **No clinical data found** for **{ta} / {dis} / {drug}**.\n\n"
                    "Without clinical data, all `{{placeholder}}` values will appear as "
                    "`{{placeholder}} [NOT FILLED]` and will need manual editing.\n\n"
                    "**Options:**\n"
                    "- Say **yes, proceed** to generate content with empty placeholders.\n"
                    "- Say **cancel** to stop and upload clinical data first."
                ),
                state_patch={
                    "awaiting_write_confirm": {"therapeutic_area": ta, "disease_type": dis, "drug_name": drug},
                },
            )
        summary = _summarise_clinical_manifest(manifest)
        msg_prefix = f"{summary}\n\n✅ Content generation will use this real clinical data. Queuing now…\n\n"
    else:
        msg_prefix = "⚠️ Proceeding without clinical data — placeholders will appear as `[NOT FILLED]`.\n\n"

    try:
        run_id = _publish_content_generation(bucket, ta, dis, drug, req.session_id, req.author)
    except Exception as exc:
        logger.error("[trigger] Pub/Sub publish failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to queue content job: {exc}") from exc

    patch = {
        "awaiting_write_confirm": None,
        "content_run_id":  run_id,
        "content_program": {"therapeutic_area": ta, "disease_type": dis, "drug_name": drug},
    }
    return ActionResponse(
        reply=(
            f"{msg_prefix}"
            f"⏳ Content generation queued for **{ta} / {dis} / {drug}** (run `{run_id[:8]}…`).\n\n"
            "The worker will:\n"
            "1. Query ICH guidelines (index service)\n"
            "2. Load the clinical datasets listed above\n"
            "3. Generate section templates and fill placeholders with real data\n"
            "4. Validate all CTD sections\n\n"
            "This usually takes 5–10 minutes. Ask me **content status** to check progress."
        ),
        state_patch=patch,
    )


@app.get("/content_status", response_model=ActionResponse)
def content_status(
    ta: str, dis: str, drug: str,
    bucket: str = _DEFAULT_BUCKET,
) -> ActionResponse:
    """Return current content-generation status for a program."""
    job    = _load_content_status(bucket, ta, dis, drug)
    status = job.get("status")

    if status == "running":
        pass_label = job.get("pass_label", "")
        pass_num   = job.get("pass_number", "")
        total      = job.get("total_passes", 3)
        step_info  = f"Pass {pass_num}/{total}: **{pass_label}**" if pass_label else "Initialising…"
        return ActionResponse(
            reply=(
                f"⏳ **Content generation is running** — {step_info}\n\n"
                "Ask me **status** again in a minute to check progress."
            ),
            state_patch={},
        )
    if status == "done":
        return ActionResponse(
            reply=(
                "✅ **Content generation is complete.** "
                "All three passes (Module 5 → 2.7 → 2.5 overviews) finished successfully.\n\n"
                "Say **generate content** to regenerate with updated clinical data."
            ),
            state_patch={"content_run_id": None},
        )
    if status == "failed":
        return ActionResponse(
            reply=(
                f"❌ **Content generation failed** — {job.get('error', 'unknown')}\n\n"
                "Say **generate content** to re-run."
            ),
            state_patch={"content_run_id": None},
        )
    return ActionResponse(
        reply=(
            f"No content generation job has been queued yet for **{ta} / {dis} / {drug}**.\n\n"
            "Say **generate content** to start the 3-pass pipeline."
        ),
        state_patch={},
    )


# ── Schema-evolution diff ─────────────────────────────────────────────────────


class SchemaDiffRequest(BaseModel):
    therapeutic_area: str
    disease_type: str
    drug_name: str
    bucket: str | None = Field(default=None)


class SchemaDiffResponse(BaseModel):
    added_placeholders: list[str] = Field(default_factory=list)
    changed_placeholders: list[str] = Field(default_factory=list)
    removed_placeholders: list[str] = Field(default_factory=list)
    affected_sections: list[str] = Field(default_factory=list)
    has_changes: bool = False


def _extract_placeholder_map(manifest: dict) -> dict[str, dict]:
    """Extract {placeholder_key: {role, ctd_section_keys}} from a manifest."""
    result: dict[str, dict] = {}
    for source in manifest.get("sources", []):
        for cm in source.get("column_mappings", []):
            pk = cm.get("placeholder_key", "")
            if pk:
                result[pk] = {
                    "role":             cm.get("role", ""),
                    "ctd_section_keys": sorted(cm.get("ctd_section_keys", [])),
                }
    return result


@app.post("/schema-diff", response_model=SchemaDiffResponse)
def schema_diff(req: SchemaDiffRequest) -> SchemaDiffResponse:
    """Compare the current manifest against its last snapshot.

    Returns which placeholder keys were added, changed, or removed since the
    previous CSV upload, along with the affected CTD sections.  The content_worker
    calls this after an upload to decide whether a schema-patch re-write pass is
    needed.  When no snapshot exists (first upload) the response reports
    ``has_changes=False``.
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
                f"{req.therapeutic_area}/{req.disease_type}/{req.drug_name}."
            ),
        )

    snap_path = f"{prefix}/clinical_data/manifest_snapshot.json"
    try:
        snap_blob = _gcs_client().bucket(bucket).blob(snap_path)
        if not snap_blob.exists():
            # First upload — no previous schema to compare against
            return SchemaDiffResponse()
        snapshot = json.loads(snap_blob.download_as_bytes())
    except Exception as exc:
        logger.warning("[schema-diff] Could not load snapshot: %s", exc)
        return SchemaDiffResponse()

    current_map  = _extract_placeholder_map(manifest)
    snapshot_map = _extract_placeholder_map(snapshot)

    added   = sorted(pk for pk in current_map  if pk not in snapshot_map)
    removed = sorted(pk for pk in snapshot_map if pk not in current_map)
    changed = sorted(
        pk for pk in current_map
        if pk in snapshot_map and current_map[pk] != snapshot_map[pk]
    )

    affected: set[str] = set()
    for pk in added + changed:
        affected.update(current_map[pk]["ctd_section_keys"])
    for pk in removed:
        affected.update(snapshot_map[pk]["ctd_section_keys"])

    return SchemaDiffResponse(
        added_placeholders=added,
        changed_placeholders=changed,
        removed_placeholders=removed,
        affected_sections=sorted(affected),
        has_changes=bool(added or changed or removed),
    )


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8084"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
