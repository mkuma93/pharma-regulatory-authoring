"""Audit event emitter — BigQuery-ready traceability layer.

Every service that generates an artifact (writer, content-worker, clinical-analyst)
calls one of the typed helpers below.  Events are:

  1. Always logged as structured JSON so Cloud Logging / Log Analytics can pick them up
     today, at zero cost, with no config.
  2. Optionally streamed to BigQuery when BQ_AUDIT_DATASET is set.

To enable BigQuery streaming (no code changes needed):
  • Create the dataset and table (DDL in BQ_TABLE_SCHEMA below).
  • Set env vars on each Cloud Run service:
      BQ_AUDIT_DATASET=<dataset_id>          e.g. regulatory_audit
      GCP_PROJECT_ID=<project>               already set on Cloud Run

Event types and the BQ columns they populate:
  document_version      — every time writer saves a new document snapshot
  generation_run        — one row per completed content-generation run
  validation_issue      — one row per ValidationIssue in the final report
  function_registered   — when clinical-analyst generates & persists a new executor
  placeholder_resolved  — one row per placeholder resolved by clinical-analyst

BQ_TABLE_SCHEMA (run once to create the table):
  CREATE TABLE IF NOT EXISTS `<project>.<dataset>.audit_events`
  (
    event_type          STRING   NOT NULL,
    emitted_at          TIMESTAMP NOT NULL,
    run_id              STRING,
    author              STRING,
    -- program context --
    therapeutic_area    STRING,
    disease_type        STRING,
    drug_name           STRING,
    -- artifact identity --
    module_key          STRING,
    section_key         STRING,
    version             INT64,
    gcs_path            STRING,
    -- validation --
    validation_passed   BOOL,
    validation_issue_count INT64,
    severity            STRING,
    issue_message       STRING,
    -- computation / provenance --
    comp_type           STRING,
    fn_name             STRING,
    placeholder_key     STRING,
    resolved_value      STRING,
    csv_source          STRING,
    -- generation stats --
    sections_written    INT64,
    sections_failed     JSON,
    -- session identity (generation_start events) --
    session_id          STRING
  )
  PARTITION BY DATE(emitted_at)
  CLUSTER BY event_type, therapeutic_area, drug_name;
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── BigQuery client (lazy singleton) ─────────────────────────────────────────

_bq_client = None


def _bq() -> Any:
    global _bq_client
    if _bq_client is None:
        from google.cloud import bigquery  # noqa: PLC0415
        _bq_client = bigquery.Client(
            project=os.environ.get("GCP_PROJECT_ID", "pharma-reguatory-author")
        )
    return _bq_client


# ── Core emitter ─────────────────────────────────────────────────────────────

def emit_audit_event(event: dict) -> None:
    """Emit a single audit event.

    Always writes a structured JSON log line.
    Also streams to BigQuery if BQ_AUDIT_DATASET env var is set (non-fatal).
    """
    event.setdefault("emitted_at", datetime.now(timezone.utc).isoformat())
    logger.info("[audit] %s", json.dumps(event))

    dataset = os.environ.get("BQ_AUDIT_DATASET", "").strip()
    if not dataset:
        return  # BQ not configured — structured log is enough for now

    project = os.environ.get("GCP_PROJECT_ID", "pharma-reguatory-author")
    table   = f"{project}.{dataset}.audit_events"

    # BigQuery expects TIMESTAMP as an ISO string; BOOL as Python bool; JSON as str.
    row = {k: v for k, v in event.items() if v is not None}
    if "sections_failed" in row and isinstance(row["sections_failed"], list):
        row["sections_failed"] = json.dumps(row["sections_failed"])

    try:
        errors = _bq().insert_rows_json(table, [row])
        if errors:
            logger.warning("[audit] BQ insert errors for table %s: %s", table, errors)
    except Exception as exc:
        logger.warning("[audit] BQ emit failed (non-fatal): %s", exc)


# ── Typed helpers (one per event type) ───────────────────────────────────────

def emit_document_version(
    *,
    run_id: str,
    author: str,
    therapeutic_area: str,
    disease_type: str,
    drug_name: str,
    module_key: str,
    section_key: str,
    version: int,
    gcs_path: str,
) -> None:
    """Emit when the writer snapshots a new document version."""
    emit_audit_event({
        "event_type":       "document_version",
        "run_id":           run_id,
        "author":           author or "system",
        "therapeutic_area": therapeutic_area,
        "disease_type":     disease_type,
        "drug_name":        drug_name,
        "module_key":       module_key,
        "section_key":      section_key,
        "version":          version,
        "gcs_path":         gcs_path,
    })


def emit_generation_start(
    *,
    run_id: str,
    author: str,
    therapeutic_area: str,
    disease_type: str,
    drug_name: str,
    session_id: str,
) -> None:
    """Emit when a content-generation run is first accepted by the worker."""
    emit_audit_event({
        "event_type":       "generation_start",
        "run_id":           run_id,
        "author":           author or "system",
        "therapeutic_area": therapeutic_area,
        "disease_type":     disease_type,
        "drug_name":        drug_name,
        "session_id":       session_id,
    })


def emit_generation_run(
    *,
    run_id: str,
    author: str,
    therapeutic_area: str,
    disease_type: str,
    drug_name: str,
    sections_written: int,
    sections_failed: list[str],
    validation_passed: bool,
    validation_issue_count: int,
) -> None:
    """Emit when a full content-generation run completes."""
    emit_audit_event({
        "event_type":            "generation_run",
        "run_id":                run_id,
        "author":                author or "system",
        "therapeutic_area":      therapeutic_area,
        "disease_type":          disease_type,
        "drug_name":             drug_name,
        "sections_written":      sections_written,
        "sections_failed":       sections_failed,
        "validation_passed":     validation_passed,
        "validation_issue_count": validation_issue_count,
    })


def emit_validation_issue(
    *,
    run_id: str,
    author: str,
    therapeutic_area: str,
    disease_type: str,
    drug_name: str,
    section_key: str,
    severity: str,
    issue_message: str,
) -> None:
    """Emit once per ValidationIssue in the final validation report."""
    emit_audit_event({
        "event_type":       "validation_issue",
        "run_id":           run_id,
        "author":           author or "system",
        "therapeutic_area": therapeutic_area,
        "disease_type":     disease_type,
        "drug_name":        drug_name,
        "section_key":      section_key,
        "severity":         severity,
        "issue_message":    issue_message,
    })


def emit_function_registered(
    *,
    run_id: str,
    author: str,
    comp_type: str,
    fn_name: str,
) -> None:
    """Emit when clinical-analyst generates and persists a new executor function."""
    emit_audit_event({
        "event_type": "function_registered",
        "run_id":     run_id,
        "author":     author or "system",
        "comp_type":  comp_type,
        "fn_name":    fn_name,
    })


def emit_placeholder_resolved(
    *,
    run_id: str,
    author: str,
    therapeutic_area: str,
    disease_type: str,
    drug_name: str,
    placeholder_key: str,
    comp_type: str,
    fn_name: str,
    csv_source: str,
    resolved_value: str = "",
) -> None:
    """Emit once per placeholder resolved by clinical-analyst."""
    emit_audit_event({
        "event_type":       "placeholder_resolved",
        "run_id":           run_id,
        "author":           author or "system",
        "therapeutic_area": therapeutic_area,
        "disease_type":     disease_type,
        "drug_name":        drug_name,
        "placeholder_key":  placeholder_key,
        "comp_type":        comp_type,
        "fn_name":          fn_name,
        "csv_source":       csv_source,
        "resolved_value":   resolved_value[:500] if resolved_value else "",  # truncate for BQ
    })
