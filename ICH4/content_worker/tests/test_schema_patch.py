"""Tests for POST /schema-patch in content_worker/worker_app.py.

Covers:
  - /schema-patch → no changes detected → returns no_changes
  - /schema-patch → changes detected → calls writer /patch → returns patched
  - /schema-patch → no clinical-analyst URL → returns skipped
  - /schema-patch → schema-diff 404 → returns no_manifest
  - /schema-patch → schema-diff error → returns error
  - /schema-patch → writer /patch error → returns error
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_mock_gcs = MagicMock()
with patch("google.cloud.storage.Client", return_value=_mock_gcs), \
     patch("pydantic.BaseModel"):
    pass

with patch("google.cloud.storage.Client", return_value=_mock_gcs):
    from worker_app import app  # noqa: E402

client = TestClient(app)

# ── Shared request / response fixtures ───────────────────────────────────────

_PATCH_REQ = {
    "therapeutic_area": "neurology",
    "disease_type":     "bells_palsy",
    "drug_name":        "prednisolone",
    "bucket":           "test-bucket",
}

_DIFF_NO_CHANGES = {
    "added_placeholders":   [],
    "changed_placeholders": [],
    "removed_placeholders": [],
    "affected_sections":    [],
    "has_changes":          False,
}

_DIFF_WITH_CHANGES = {
    "added_placeholders":   ["adverse_events"],
    "changed_placeholders": ["recovery_rate_3mo"],
    "removed_placeholders": [],
    "affected_sections":    ["2.7.3", "5.3.5.1"],
    "has_changes":          True,
}

_WRITER_PATCH_RESPONSE = {
    "documents":        [],
    "sections_written": 2,
    "sections_failed":  [],
    "validation":       {"passed": True, "issues": [], "summary": "ok"},
}


def _make_http_response(status_code: int, body: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def _async_client_posting(*responses) -> MagicMock:
    """Build an async HTTP client that returns responses in order."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=list(responses))
    return mock_client


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSchemaPatchSkipped:
    def test_skipped_when_no_analyst_url(self):
        with patch("worker_app._CLINICAL_ANALYST_URL", ""), \
             patch("worker_app._WRITER_URL", "https://writer.run.app"):
            resp = client.post("/schema-patch", json=_PATCH_REQ)
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_skipped_when_no_writer_url(self):
        with patch("worker_app._CLINICAL_ANALYST_URL", "https://analyst.run.app"), \
             patch("worker_app._WRITER_URL", ""):
            resp = client.post("/schema-patch", json=_PATCH_REQ)
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"


class TestSchemaPatchNoChanges:
    def test_returns_no_changes_when_diff_empty(self):
        diff_resp  = _make_http_response(200, _DIFF_NO_CHANGES)
        mock_ac    = _async_client_posting(diff_resp)

        with patch("worker_app._CLINICAL_ANALYST_URL", "https://analyst.run.app"), \
             patch("worker_app._WRITER_URL",           "https://writer.run.app"), \
             patch("worker_app._oidc_headers",         return_value={}), \
             patch("httpx.AsyncClient",                return_value=mock_ac):
            resp = client.post("/schema-patch", json=_PATCH_REQ)

        assert resp.status_code == 200
        assert resp.json()["status"] == "no_changes"


class TestSchemaPatchNoManifest:
    def test_returns_no_manifest_on_404(self):
        diff_resp = _make_http_response(404, {"detail": "No manifest"})
        mock_ac   = _async_client_posting(diff_resp)

        with patch("worker_app._CLINICAL_ANALYST_URL", "https://analyst.run.app"), \
             patch("worker_app._WRITER_URL",           "https://writer.run.app"), \
             patch("worker_app._oidc_headers",         return_value={}), \
             patch("httpx.AsyncClient",                return_value=mock_ac):
            resp = client.post("/schema-patch", json=_PATCH_REQ)

        assert resp.status_code == 200
        assert resp.json()["status"] == "no_manifest"


class TestSchemaPatchHappyPath:
    def test_returns_patched_with_summary(self):
        diff_resp   = _make_http_response(200, _DIFF_WITH_CHANGES)
        writer_resp = _make_http_response(200, _WRITER_PATCH_RESPONSE)
        mock_ac     = _async_client_posting(diff_resp, writer_resp)

        with patch("worker_app._CLINICAL_ANALYST_URL", "https://analyst.run.app"), \
             patch("worker_app._WRITER_URL",           "https://writer.run.app"), \
             patch("worker_app._oidc_headers",         return_value={}), \
             patch("httpx.AsyncClient",                return_value=mock_ac):
            resp = client.post("/schema-patch", json=_PATCH_REQ)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]             == "patched"
        assert data["sections_written"]   == 2
        assert data["sections_failed"]    == []
        assert data["affected_sections"]  == ["2.7.3", "5.3.5.1"]
        assert "adverse_events"           in data["added_placeholders"]
        assert "recovery_rate_3mo"        in data["changed_placeholders"]
        assert data["validation_passed"]  is True

    def test_changed_keys_forwarded_to_writer(self):
        diff_resp   = _make_http_response(200, _DIFF_WITH_CHANGES)
        writer_resp = _make_http_response(200, _WRITER_PATCH_RESPONSE)
        mock_ac     = _async_client_posting(diff_resp, writer_resp)

        with patch("worker_app._CLINICAL_ANALYST_URL", "https://analyst.run.app"), \
             patch("worker_app._WRITER_URL",           "https://writer.run.app"), \
             patch("worker_app._oidc_headers",         return_value={}), \
             patch("httpx.AsyncClient",                return_value=mock_ac):
            client.post("/schema-patch", json=_PATCH_REQ)

        # Second POST call is the writer /patch — check what was sent
        writer_call_body = mock_ac.post.call_args_list[1].kwargs["json"]
        assert set(writer_call_body["changed_keys"]) == {"adverse_events", "recovery_rate_3mo"}
        assert writer_call_body["sections"] == ["2.7.3", "5.3.5.1"]


class TestSchemaPatchErrors:
    def test_diff_5xx_returns_error(self):
        diff_resp = _make_http_response(500, {"detail": "Internal error"})
        mock_ac   = _async_client_posting(diff_resp)

        with patch("worker_app._CLINICAL_ANALYST_URL", "https://analyst.run.app"), \
             patch("worker_app._WRITER_URL",           "https://writer.run.app"), \
             patch("worker_app._oidc_headers",         return_value={}), \
             patch("httpx.AsyncClient",                return_value=mock_ac):
            resp = client.post("/schema-patch", json=_PATCH_REQ)

        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

    def test_writer_error_returns_error(self):
        diff_resp   = _make_http_response(200, _DIFF_WITH_CHANGES)
        writer_resp = _make_http_response(500, {"detail": "Writer crashed"})
        mock_ac     = _async_client_posting(diff_resp, writer_resp)

        with patch("worker_app._CLINICAL_ANALYST_URL", "https://analyst.run.app"), \
             patch("worker_app._WRITER_URL",           "https://writer.run.app"), \
             patch("worker_app._oidc_headers",         return_value={}), \
             patch("httpx.AsyncClient",                return_value=mock_ac):
            resp = client.post("/schema-patch", json=_PATCH_REQ)

        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

    def test_network_error_returns_error(self):
        import httpx as _httpx

        mock_ac = AsyncMock()
        mock_ac.__aenter__ = AsyncMock(return_value=mock_ac)
        mock_ac.__aexit__  = AsyncMock(return_value=False)
        mock_ac.post = AsyncMock(side_effect=_httpx.ConnectError("refused"))

        with patch("worker_app._CLINICAL_ANALYST_URL", "https://analyst.run.app"), \
             patch("worker_app._WRITER_URL",           "https://writer.run.app"), \
             patch("worker_app._oidc_headers",         return_value={}), \
             patch("httpx.AsyncClient",                return_value=mock_ac):
            resp = client.post("/schema-patch", json=_PATCH_REQ)

        assert resp.status_code == 200
        assert resp.json()["status"] == "error"
