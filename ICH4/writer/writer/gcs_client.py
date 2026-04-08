"""Shared thread-safe GCS client and program prefix helper."""
from __future__ import annotations

import threading

from google.cloud import storage

from .models import ProgramInfo

_lock = threading.Lock()
_client: storage.Client | None = None


def gcs() -> storage.Client:
    """Return a module-level GCS client (thread-safe lazy init)."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = storage.Client()
    return _client


def program_prefix(program: ProgramInfo) -> str:
    ta   = program.therapeutic_area.strip().lower().replace(" ", "_")
    dis  = program.disease_type.strip().lower().replace(" ", "_")
    drug = program.drug_name.strip().lower().replace(" ", "_")
    return f"therapeutic-area/{ta}/{dis}/{drug}"
