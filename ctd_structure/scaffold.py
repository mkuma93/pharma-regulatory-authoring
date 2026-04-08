"""
Scaffold helpers for the CTD folder hierarchy.

Low-level helpers
─────────────────
  scaffold_template(folder_paths) — create the canonical CTD folder tree
                                    once under ctd_structure/ctd/ (the template).
  copy_to_program(destination)    — copy that template into a program dir:
                                    programs/{therapeutic_area}/{disease_type}/{drug}/ctd/
  scaffold_in_gcs(bucket, …)      — create .keep blobs in a GCS bucket.
  scaffold_locally(base_dir, …)   — create the hierarchy on the local filesystem.

High-level entry point
──────────────────────
  run(ich_index_url, reviewer_email) — full interactive workflow:
    1. Extract canonical ICH CTD structure from the index service.
    2. Show proposed folder tree + evaluation to the user.
    3. Prompt APPROVE / REJECT in the terminal.
    4. Create the canonical template under ctd_structure/ctd/.
    5. Prompt for therapeutic area, disease type, and drug name.
    6. Copy the template into programs/{ta}/{disease}/{drug}/ctd/.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

from google.cloud import storage

# Canonical template location.
# On Cloud Run only /tmp is writable, so we default there when the package
# directory itself is read-only (e.g. installed in site-packages).
_DEFAULT_TEMPLATE_ROOT = Path(__file__).resolve().parent
if not os.access(_DEFAULT_TEMPLATE_ROOT, os.W_OK):
    _DEFAULT_TEMPLATE_ROOT = Path("/tmp/ctd_structure")

_TEMPLATE_DIR: Path = _DEFAULT_TEMPLATE_ROOT / "ctd"


def scaffold_template(folder_paths: list[str]) -> Path:
    """
    Create the canonical CTD folder tree under ctd_structure/ctd/.

    This is the single template that gets copied per program.
    Safe to call multiple times — existing folders are left untouched.

    Args:
        folder_paths: Relative paths from CTDStructureOutput.to_folder_paths(),
                      e.g. ["ctd/module1/", "ctd/module1/1.1_table_of_contents/", ...].

    Returns:
        Path to the template root (ctd_structure/ctd/).
    """
    created = 0
    for folder in folder_paths:
        # folder already starts with "ctd/...", place directly under _TEMPLATE_DIR.parent
        target = _TEMPLATE_DIR.parent / folder
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            print(f"  created  {target}")
            created += 1
        else:
            print(f"  exists   {target}")
    print(f"\n  {created} folder(s) created in template ({_TEMPLATE_DIR})")
    return _TEMPLATE_DIR


def copy_to_program(destination: Path) -> None:
    """
    Copy the canonical CTD template into a program directory.

    Copies ctd_structure/ctd/ → {destination}/ctd/

    Args:
        destination: Program root, e.g.
                     Path("programs/neurology/bells_palsy/prednisolone").
    """
    src = _TEMPLATE_DIR
    dst = destination / "ctd"
    if not src.exists():
        raise FileNotFoundError(
            f"CTD template not found at {src}. "
            "Run scaffold_template() first."
        )
    if dst.exists():
        print(f"  exists   {dst}  (skipped)")
        return
    shutil.copytree(src, dst)
    print(f"  copied   {src}  →  {dst}")


def scaffold_in_gcs(bucket: storage.Bucket, base: str, folder_paths: list[str]) -> None:
    """
    Create `.keep` marker blobs for each folder path under ``base`` in GCS.

    Args:
        bucket:       GCS Bucket object (already authenticated).
        base:         Program base path, e.g. "programs/neurology/bells_palsy/prednisolone".
        folder_paths: List of relative paths from ``flatten_to_folder_paths()``,
                      e.g. ["ctd/module1/", "ctd/module1/1.1_table_of_contents/", ...].
    """
    for folder in folder_paths:
        blob_path = f"{base}/{folder}.keep"
        blob = bucket.blob(blob_path)
        if not blob.exists():
            blob.upload_from_string(b"", content_type="application/octet-stream")
            print(f"  created  gs://{bucket.name}/{base}/{folder}")
        else:
            print(f"  exists   gs://{bucket.name}/{base}/{folder}")

    # Update workflow/status.json
    status_blob = bucket.blob(f"{base}/workflow/status.json")
    try:
        current = json.loads(status_blob.download_as_text())
    except Exception:
        current = {}
    current.update({"ctd_created": True, "ctd_created_date": str(date.today())})
    status_blob.upload_from_string(
        json.dumps(current, indent=2),
        content_type="application/json",
    )
    print(f"\n  updated  gs://{bucket.name}/{base}/workflow/status.json")


def scaffold_locally(base_dir: Path, folder_paths: list[str]) -> None:
    """
    Create the CTD folder hierarchy on the local filesystem under ``base_dir``.

    Useful for local development / testing without GCS.

    Args:
        base_dir:     Root directory to create folders in (e.g. Path("ctd")).
        folder_paths: List of relative paths from ``flatten_to_folder_paths()``.
    """
    for folder in folder_paths:
        target = base_dir / folder
        target.mkdir(parents=True, exist_ok=True)
        print(f"  created  {target}")


# ── Interactive orchestration ──────────────────────────────────────────────────

_PROGRAMS_DIR = Path(__file__).resolve().parent.parent / "programs"


def _print_proposed_structure(folder_paths: list[str]) -> None:
    sep = "─" * 60
    print(f"\n{sep}")
    print("  Proposed ICH CTD folder structure")
    print(sep)
    for f in folder_paths:
        depth = f.rstrip("/").count("/") - 1
        name  = f.rstrip("/").rsplit("/", 1)[-1]
        print(f"  {'  ' * depth}{name}/")
    print(sep)


def run(
    ich_index_url: str,
    reviewer_email: str | None = None,
    programs_dir: Path | None = None,
) -> None:
    """
    Full interactive workflow — extract, validate, scaffold, copy.

    Steps
    -----
    1. Call ``extract_from_ich_index()`` — queries the ICH4/index service
       (LLM-backed) for every module, section, and subsection.
    2. Display the resulting folder tree and evaluation report.
    3. Prompt the user to APPROVE or REJECT in the terminal.
    4. On approval: run ``scaffold_template()`` to create the canonical
       template under ``ctd_structure/ctd/``.
    5. Prompt for therapeutic area, disease type, and drug name.
    6. Run ``copy_to_program()`` to copy the template into
       ``programs/{ta}/{disease}/{drug}/ctd/``.

    Args:
        ich_index_url:  Base URL of the deployed ICH4/index Cloud Run service.
        reviewer_email: Optional email for the approval notification sent
                        by ``notify_human`` inside the graph.
        programs_dir:   Override the default ``programs/`` directory root
                        (useful for testing).
    """
    # Import here to avoid a circular import at module level
    # (structure.py does not import scaffold.py)
    from ctd_structure.structure import extract_from_ich_index  # noqa: PLC0415

    base = programs_dir or _PROGRAMS_DIR

    # ── Step 1: extract ───────────────────────────────────────────────────────
    print("\n=== CTD Structure Agent ===")
    print(f"  ICH API : {ich_index_url}")
    print("\n[1/4] Extracting canonical ICH CTD structure from index...")

    ctd_output, evaluation = extract_from_ich_index(
        ich_index_url=ich_index_url,
        reviewer_email=reviewer_email,
    )
    folder_paths = ctd_output.to_folder_paths()
    print(f"  {len(folder_paths)} folder path(s) extracted")

    # ── Step 2: show evaluation + proposed tree ───────────────────────────────
    print(f"\n[2/4] Evaluation: {'PASSED' if evaluation.passed else 'ISSUES FOUND'}")
    print(f"  {evaluation.summary}")
    _print_proposed_structure(folder_paths)

    # ── Step 3: terminal validation ───────────────────────────────────────────
    if not evaluation.passed:
        print("WARNING: the evaluator found completeness issues above.")
    answer = input("Approve this structure and create folders? [y/N]: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    # ── Step 4: scaffold canonical template under ctd_structure/ctd/ ──────────
    print("\n[3/4] Scaffolding canonical template under ctd_structure/ctd/...")
    scaffold_template(folder_paths)

    # ── Step 5: prompt for program destination ────────────────────────────────
    print("\n[4/4] Where should this structure be copied?")
    ta     = input("  Therapeutic area (e.g. neurology):    ").strip().lower().replace(" ", "_")
    dtype  = input("  Disease type     (e.g. bells_palsy):  ").strip().lower().replace(" ", "_")
    drug   = input("  Drug name        (e.g. prednisolone): ").strip().lower().replace(" ", "_")

    if not all([ta, dtype, drug]):
        print("ERROR: all three fields are required. Aborted.")
        sys.exit(1)

    program_dir = base / ta / dtype / drug
    print(f"\n  Copying template → programs/{ta}/{dtype}/{drug}/ctd/...")
    copy_to_program(program_dir)

    print("\nDone.")
    print(f"  Template : ctd_structure/ctd/")
    print(f"  Program  : {program_dir}/ctd/")
