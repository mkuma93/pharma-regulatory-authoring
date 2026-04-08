"""
conftest.py for ICH4/index/tests/

Sets required env vars and stubs out heavy llama_index / llama_parse packages
BEFORE any application module is imported during pytest collection.

Run from the ICH4/index/ directory so api/, knowledge/, and config/ are on
the default Python path:
  cd ICH4/index && python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# ── Required env vars (must be set before config.settings is first imported) ──
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("LLAMA_CLOUD_API_KEY", "test-llama-key")
os.environ.setdefault("GCP_PROJECT_ID", "")      # empty → skip Secret Manager
os.environ.setdefault("GCS_BUCKET_NAME", "")     # empty → skip GCS index download
os.environ.setdefault("GCS_INDEX_PREFIX", "index_store")
os.environ.setdefault("IAP_AUDIENCE", "")        # empty → IAP middleware pass-through
os.environ.setdefault("INDEX_PERSIST_DIR", "/tmp/test_index_store")

# ── Stub heavy packages not installed in the test environment ─────────────────
# llama_index and llama_parse are large ML packages replaced with MagicMocks so
# the application code can be imported and tested without them.
_LLAMA_STUBS = [
    "llama_index",
    "llama_index.core",
    "llama_index.core.query_engine",
    "llama_index.core.vector_stores",
    "llama_index.core.vector_stores.types",
    "llama_index.llms",
    "llama_index.llms.openai",
    "llama_index.embeddings",
    "llama_index.embeddings.openai",
    "llama_parse",
]
for _mod in _LLAMA_STUBS:
    sys.modules.setdefault(_mod, MagicMock())
