"""writer/clinical_uploader.py

Upload a clinical CSV for a specific program (drug) into GCS and auto-build
the column manifest so the writer service knows which dataset belongs to which
drug.

Flow
────
  1. Store the CSV bytes at
       {bucket}/{program_prefix}/clinical_data/{filename}
  2. Run the LLM column mapper (gpt-4o-mini) to identify column roles and
     CTD section keys — same logic as clinical/mapper.py but self-contained
     so the writer service has no cross-package dependencies.
  3. Load (or create) the program's manifest.json from GCS and upsert this
     source entry (replace by filename if it already exists).
  4. Save the updated manifest back to GCS.
  5. Return an UploadResult with the GCS path and detected metadata.

Manifest schema (matches ClinicalDataManifest used by clinical_reader.py)
──────────────────────────────────────────────────────────────────────────
  {
    "program": { "therapeutic_area": ..., "disease_type": ..., "drug_name": ... },
    "sources": [
      {
        "filename": "Bells Palsy Clinical Trial.csv",
        "gcs_path": "therapeutic-area/.../clinical_data/Bells Palsy Clinical Trial.csv",
        "study_type": "RCT",
        "column_mappings": [...],
        "ctd_section_keys": [...]
      }
    ]
  }
"""
from __future__ import annotations

import csv
import io
import json
import logging

from json_repair import repair_json
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .gcs_client import gcs, program_prefix
from .models import ProgramInfo

logger = logging.getLogger(__name__)

# ── LLM mapper prompts (self-contained copy — no dep on life-science/clinical/) ──

_MAPPER_SYSTEM = """\
You are a senior research scientist with deep expertise in clinical drug development
and ICH CTD regulatory submissions.

Your task is to analyse a clinical dataset and map each column to its regulatory role
and the CTD sections where its data would appear.

ICH M4 section key conventions (short numeric keys only):
  Module 2 summaries : 2.5, 2.7.1–2.7.6
  Module 5 study data: 5.2, 5.3.5.1, 5.3.5.2, 5.3.7, 5.4

Placeholder naming: use snake_case, be specific (e.g. 'recovery_rate_3mo' not 'rate').

Return ONLY a valid JSON object — no prose, no markdown fences.
"""

_MAPPER_USER_TMPL = """\
Dataset context
───────────────
  Program : {drug_name} / {disease_type} / {therapeutic_area}
  Filename: {filename}
  Rows    : {n_rows}

Column headers with up to {n_sample} sample values each
────────────────────────────────────────────────────────
{column_samples}

Task
────
1. Set study_type: "RCT" | "registry" | "observational" | "PK" | "safety" | "other"

2. For each column relevant to a CTD submission produce a ColumnMapping:
     column_name      – exact header
     role             – "efficacy_endpoint" | "safety_endpoint" | "demographics" |
                        "treatment_group" | "study_metadata" | "pharmacokinetics" | "other"
     ctd_section_keys – list of section keys
     placeholder_key  – snake_case template placeholder name

   Skip columns with no regulatory meaning (row IDs, internal codes).

3. Set top-level ctd_section_keys as sorted union of all column ctd_section_keys.

Return exactly:
{{
  "filename": "{filename}",
  "study_type": "...",
  "column_mappings": [
    {{"column_name":"...","role":"...","ctd_section_keys":[...],"placeholder_key":"..."}}
  ],
  "ctd_section_keys": [...]
}}
"""


# ── Response model ────────────────────────────────────────────────────────────

class UploadResult(BaseModel):
    gcs_path: str
    filename: str
    study_type: str
    ctd_section_keys: list[str]
    columns_mapped: int
    columns: int          # alias for UI compatibility
    total_sources: int    # number of CSVs in the manifest after this upload
    manifest_gcs_path: str


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run_mapper(
    filename: str,
    csv_text: str,
    program: ProgramInfo,
    llm: ChatOpenAI,
    n_sample: int = 5,
) -> dict:
    """Call the LLM mapper and return the parsed source dict (no gcs_path yet)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    headers: list[str] = list(reader.fieldnames or [])

    col_samples: dict[str, list[str]] = {h: [] for h in headers}
    for row in rows[:n_sample]:
        for h in headers:
            val = str(row.get(h, "")).strip()
            col_samples[h].append(val if val else "(empty)")

    column_samples_text = "\n".join(
        f"  {h!r}: {col_samples[h]}" for h in headers
    )

    prompt = _MAPPER_USER_TMPL.format(
        drug_name=program.drug_name,
        disease_type=program.disease_type,
        therapeutic_area=program.therapeutic_area,
        filename=filename,
        n_rows=len(rows),
        n_sample=n_sample,
        column_samples=column_samples_text,
    )

    response = llm.invoke([
        SystemMessage(content=_MAPPER_SYSTEM),
        HumanMessage(content=prompt),
    ])
    raw = response.content.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    # Try strict parse first; on failure, attempt repair then retry LLM once
    for attempt in range(3):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            if attempt < 2:
                # First try: repair the malformed JSON in-place
                try:
                    return json.loads(repair_json(raw))
                except Exception:
                    pass
                # Second try: ask LLM to return only clean JSON
                logger.warning(
                    "[uploader] Mapper returned invalid JSON (attempt %d), retrying LLM...",
                    attempt + 1,
                )
                response = llm.invoke([
                    SystemMessage(content=_MAPPER_SYSTEM),
                    HumanMessage(content=prompt + "\n\nIMPORTANT: Return ONLY a valid JSON object. No prose, no code fences."),
                ])
                raw = response.content.strip()
                if raw.startswith("```"):
                    raw = raw.split("```", 2)[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.rsplit("```", 1)[0].strip()
            else:
                # Final attempt: use repair_json which is very permissive
                return json.loads(repair_json(raw))
    return json.loads(raw)  # unreachable but satisfies type checkers


def _load_manifest(bucket_name: str, manifest_path: str) -> dict:
    """Load existing manifest.json from GCS, or return an empty manifest."""
    try:
        blob = gcs().bucket(bucket_name).blob(manifest_path)
        if blob.exists():
            return json.loads(blob.download_as_text())
    except Exception as exc:
        logger.warning("[uploader] Could not load manifest %s: %s", manifest_path, exc)
    return {"sources": []}


def _save_manifest(bucket_name: str, manifest_path: str, manifest: dict) -> None:
    blob = gcs().bucket(bucket_name).blob(manifest_path)
    blob.upload_from_string(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        content_type="application/json",
    )
    logger.info("[uploader] Manifest saved to gs://%s/%s", bucket_name, manifest_path)


def _snapshot_manifest(bucket_name: str, manifest_path: str) -> None:
    """Copy manifest.json → manifest_snapshot.json before overwriting.

    Called before each upload so the clinical-analyst /schema-diff endpoint can
    compare the old schema against the new one and identify affected CTD sections.
    """
    try:
        bkt  = gcs().bucket(bucket_name)
        blob = bkt.blob(manifest_path)
        if blob.exists():
            snap_path = manifest_path.rsplit("/manifest.json", 1)[0] + "/manifest_snapshot.json"
            raw = blob.download_as_bytes()
            bkt.blob(snap_path).upload_from_string(raw, content_type="application/json")
            logger.info("[uploader] Snapshot saved to gs://%s/%s", bucket_name, snap_path)
    except Exception as exc:
        logger.warning("[uploader] Could not snapshot manifest: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def upload_clinical_csv(
    bucket_name: str,
    program: ProgramInfo,
    filename: str,
    csv_bytes: bytes,
    llm: ChatOpenAI,
) -> UploadResult:
    """Upload a clinical CSV for a program/drug and (re)build the manifest.

    Steps:
      1. Upload CSV to GCS at {program_prefix}/clinical_data/{filename}
      2. Run LLM mapper to identify columns, roles, CTD sections
      3. Upsert the source entry in manifest.json (replace if filename exists)
      4. Save manifest.json back to GCS

    Args:
        bucket_name: GCS bucket that holds all program data.
        program:     Identifies therapeutic_area / disease_type / drug_name.
        filename:    Original filename of the uploaded CSV (used as the GCS blob name).
        csv_bytes:   Raw bytes of the uploaded CSV file.
        llm:         Bound ChatOpenAI instance for the column mapper.

    Returns:
        :class:`UploadResult` with GCS paths and detected metadata.
    """
    prefix = program_prefix(program)
    csv_gcs_path = f"{prefix}/clinical_data/{filename}"
    manifest_gcs_path = f"{prefix}/clinical_data/manifest.json"

    # 1. Upload CSV
    bucket = gcs().bucket(bucket_name)
    csv_blob = bucket.blob(csv_gcs_path)
    csv_blob.upload_from_string(csv_bytes, content_type="text/csv")
    logger.info("[uploader] CSV uploaded to gs://%s/%s", bucket_name, csv_gcs_path)

    # 2. Map columns via LLM
    csv_text = csv_bytes.decode("utf-8", errors="replace")
    try:
        source_dict = _run_mapper(filename, csv_text, program, llm)
    except Exception as exc:
        # Upload succeeded; mapping failed — don't block the user, log and return partial
        logger.error("[uploader] Mapper failed for %s: %s", filename, exc)
        return UploadResult(
            gcs_path=f"gs://{bucket_name}/{csv_gcs_path}",
            filename=filename,
            study_type="unknown",
            ctd_section_keys=[],
            columns_mapped=0,
            columns=0,
            total_sources=0,
            manifest_gcs_path=f"gs://{bucket_name}/{manifest_gcs_path}",
        )

    # Set the GCS path inside the source dict
    source_dict["gcs_path"] = f"gs://{bucket_name}/{csv_gcs_path}"
    source_dict.setdefault("filename", filename)

    # 3. Upsert into manifest
    manifest = _load_manifest(bucket_name, manifest_gcs_path)
    _snapshot_manifest(bucket_name, manifest_gcs_path)  # preserve old schema before overwrite
    manifest["program"] = {
        "therapeutic_area": program.therapeutic_area,
        "disease_type": program.disease_type,
        "drug_name": program.drug_name,
    }
    # Remove old entry for this filename (if any) then append updated
    manifest["sources"] = [
        s for s in manifest.get("sources", []) if s.get("filename") != filename
    ]
    manifest["sources"].append(source_dict)

    # 4. Persist manifest
    _save_manifest(bucket_name, manifest_gcs_path, manifest)

    return UploadResult(
        gcs_path=f"gs://{bucket_name}/{csv_gcs_path}",
        filename=filename,
        study_type=source_dict.get("study_type", "unknown"),
        ctd_section_keys=source_dict.get("ctd_section_keys", []),
        columns_mapped=len(source_dict.get("column_mappings", [])),
        columns=len(source_dict.get("column_mappings", [])),
        total_sources=len(manifest["sources"]),
        manifest_gcs_path=f"gs://{bucket_name}/{manifest_gcs_path}",
    )
