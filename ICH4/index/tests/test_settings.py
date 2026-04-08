"""Tests for config/settings.py — Settings class."""
from __future__ import annotations

from pathlib import Path

import pytest

# Import the class, not the singleton, so we can create fresh instances.
from config.settings import Settings


class TestSettingsFromEnv:
    def test_loads_required_fields_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        s = Settings()
        assert s.openai_api_key == "sk-openai-test"
        assert s.llama_cloud_api_key == "lc-test"

    def test_default_llm_model(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        s = Settings()
        assert s.llm_model == "gpt-4o"

    def test_default_embedding_model(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        s = Settings()
        assert s.embedding_model == "text-embedding-3-small"

    def test_default_gcs_bucket_is_empty(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        s = Settings()
        assert s.gcs_bucket_name == ""

    def test_gcs_bucket_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        monkeypatch.setenv("GCS_BUCKET_NAME", "my-bucket")
        s = Settings()
        assert s.gcs_bucket_name == "my-bucket"

    def test_gcs_index_prefix_default(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        s = Settings()
        assert s.gcs_index_prefix == "index_store"

    def test_index_persist_dir_is_path(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        s = Settings()
        assert isinstance(s.index_persist_dir, Path)

    def test_index_persist_dir_override(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        monkeypatch.setenv("INDEX_PERSIST_DIR", "/tmp/custom_store")
        s = Settings()
        assert s.index_persist_dir == Path("/tmp/custom_store")

    def test_llamaparse_result_type_default(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        s = Settings()
        assert s.llamaparse_result_type == "markdown"

    def test_gcp_project_id_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "lc-test")
        monkeypatch.setenv("GCP_PROJECT_ID", "my-project")
        # GcpSecretManagerSettingsSource tries to import secretmanager only when
        # project_id is set, but catches all exceptions — so this won't raise.
        s = Settings()
        assert s.gcp_project_id == "my-project"
