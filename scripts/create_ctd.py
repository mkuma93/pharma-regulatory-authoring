# ONE-TIME SCRIPT — run once to initialise the canonical CTD folder template.
"""
create_ctd.py — Extract the canonical ICH CTD structure and scaffold it.

Flow (orchestrated by ctd_structure.scaffold.run):
  1. Query ICH4/index (LLM) — extract all modules, sections, subsections.
  2. Show proposed folder tree + evaluation report to the user.
  3. User approves or rejects in the terminal.
  4. On approval: create the canonical template under ctd_structure/ctd/.
  5. Prompt for therapeutic area, disease type, and drug name.
  6. Copy the template into programs/{therapeutic_area}/{disease_type}/{drug}/ctd/.

Usage:
  python scripts/create_ctd.py
  python scripts/create_ctd.py --reviewer-email you@example.com
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(str(Path(__file__).resolve().parent.parent / "config" / ".env"))

from ctd_structure.scaffold import run

ICH_INDEX_URL = os.environ.get(
    "ICH_INDEX_URL",
    "https://ich4-index-your-service-id-uc.a.run.app",
)

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Scaffold CTD folder structure from ICH index.")
    p.add_argument(
        "--reviewer-email",
        default=os.environ.get("CTD_REVIEWER_EMAIL"),
        help="Email address to send the approval request to (or set CTD_REVIEWER_EMAIL).",
    )
    args = p.parse_args()

    run(ich_index_url=ICH_INDEX_URL, reviewer_email=args.reviewer_email)


