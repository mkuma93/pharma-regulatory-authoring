"""
clinical/ingestion.py

High-level entry point for registering a clinical CSV into the submission pipeline.

Workflow
────────
  1. Upload CSV bytes to GCS  →  therapeutic-area/{ta}/{disease}/{drug}/clinical_data/{filename}
  2. Read headers + sample rows from the CSV text
  3. Call LLM mapper        →  ClinicalDataSource (study_type, column_mappings, ctd_section_keys)
  4. Load existing manifest (or create a fresh one for this program)
  5. Replace existing entry for the same filename (idempotent), or append
  6. Persist updated manifest.json back to GCS
  7. Return the updated ClinicalDataManifest
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from template.models import ProgramInfo

from .mapper import map_csv_to_ctd
from .models import ClinicalDataManifest, ColumnMapping
from .storage import load_manifest, save_manifest, upload_csv


# Narrative placeholder keys required by the ICH CTD Section 2.5 template.
# Each entry maps a key → ordered list of role preferences to pick the PRIMARY column for.
_NARRATIVE_PLACEHOLDER_ROLES: dict[str, list[str]] = {
    "efficacy_overview":              ["efficacy_endpoint"],
    "primary_efficacy_endpoint":      ["efficacy_endpoint"],
    "number_of_efficacy_trials":      ["treatment_group", "study_metadata"],
    "total_safety_n":                 ["study_metadata", "treatment_group", "safety_endpoint", "demographics", "efficacy_endpoint"],
    "number_of_safety_trials":        ["treatment_group", "study_metadata"],
    "safety_overview":                ["safety_endpoint", "treatment_group", "efficacy_endpoint"],
    "benefit_risk_conclusions":       ["efficacy_endpoint"],
    "product_development_rationale":  ["efficacy_endpoint"],
}


def _inject_narrative_mappings(source: "ClinicalDataSource") -> "ClinicalDataSource":  # type: ignore[name-defined]
    """Add missing ICH 2.5 narrative placeholder mappings using existing column mappings."""
    existing_keys = {cm.placeholder_key for cm in source.column_mappings}
    extra: list[ColumnMapping] = []

    # Build role → list of ColumnMappings lookup
    by_role: dict[str, list[ColumnMapping]] = {}
    for cm in source.column_mappings:
        by_role.setdefault(cm.role, []).append(cm)

    for placeholder_key, role_priority in _NARRATIVE_PLACEHOLDER_ROLES.items():
        if placeholder_key in existing_keys:
            continue
        # Pick the first available column matching role priority
        chosen: ColumnMapping | None = None
        for role in role_priority:
            candidates = by_role.get(role, [])
            if candidates:
                chosen = candidates[0]
                break
        if chosen is None:
            # Fallback: use the very first column mapping available
            if source.column_mappings:
                chosen = source.column_mappings[0]
        if chosen:
            extra.append(ColumnMapping(
                column_name=chosen.column_name,
                role=chosen.role,
                ctd_section_keys=["2.5"],
                placeholder_key=placeholder_key,
                positive_value=None,
            ))

    if not extra:
        return source
    return source.model_copy(update={"column_mappings": source.column_mappings + extra})


def register_clinical_csv(
    bucket_name: str,
    program: ProgramInfo,
    local_csv_path: str | Path,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
) -> ClinicalDataManifest:
    """Register one clinical CSV for a drug program.

    Uploads the file to GCS, maps its columns to CTD placeholders via LLM,
    and updates the program's clinical data manifest.

    Args:
        bucket_name:    GCS bucket name.
        program:        Drug program (therapeutic area / disease / drug).
        local_csv_path: Local filesystem path to the CSV file.
        api_key:        OpenAI API key (falls back to OPENAI_API_KEY env var).
        model:          OpenAI chat model for column mapping.

    Returns:
        Updated :class:`ClinicalDataManifest` with the new source appended
        (or replaced if a source with the same filename already exists).
    """
    path      = Path(local_csv_path)
    filename  = path.name
    csv_bytes = path.read_bytes()
    csv_text  = csv_bytes.decode("utf-8-sig")

    # 1. Upload CSV to GCS
    gcs_path = upload_csv(bucket_name, program, filename, csv_bytes)

    # 2. Map columns via LLM
    source = map_csv_to_ctd(
        filename=filename,
        csv_content=csv_text,
        program=program,
        api_key=api_key,
        model=model,
    )
    # Set the real GCS path now that the file is uploaded
    source = source.model_copy(update={"gcs_path": gcs_path})

    # 2b. Inject standard ICH 2.5 narrative placeholder mappings (deterministic post-process)
    source = _inject_narrative_mappings(source)

    # 3. Load or initialise manifest
    manifest = load_manifest(bucket_name, program) or ClinicalDataManifest(program=program)

    # 4. Replace existing entry for the same filename (idempotent on re-upload)
    updated_sources = [s for s in manifest.sources if s.filename != filename]
    updated_sources.append(source)

    manifest = manifest.model_copy(
        update={
            "sources":      updated_sources,
            "last_updated": date.today().isoformat(),
        }
    )

    # 5. Persist
    save_manifest(bucket_name, manifest)

    return manifest
