"""
Tests for ICH4 content_worker/worker_app.py

Covers:
  - GET /health
  - POST /generate  – happy path (orchestrator + writer succeed)
  - POST /generate  – malformed Pub/Sub envelope → ack (200)
  - POST /generate  – missing required fields → ack (200)
  - POST /generate  – duplicate run_id (already done) → 200 duplicate
  - POST /generate  – orchestrator 4xx → ack (200 failed)
  - POST /generate  – orchestrator 5xx → raise (Pub/Sub retry)
  - POST /generate  – writer 4xx → ack (200 failed)
  - POST /generate  – writer 5xx → raise (Pub/Sub retry)
  - POST /generate  – ConnectError → raise (Pub/Sub retry)
  - _program_prefix slugging
  - _oidc_headers falls back gracefully on missing credentials
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

# ── Patch GCS before importing app so _gcs_client is mocked from the start ──
_mock_gcs = MagicMock()
with patch("google.cloud.storage.Client", return_value=_mock_gcs):
    from worker_app import _program_prefix, _oidc_headers, app  # noqa: E402

client = TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pubsub_body(payload: dict) -> dict:
    """Wrap a dict in a Pub/Sub push envelope."""
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"message": {"data": data, "messageId": "test-123"}}


_GOOD_PAYLOAD = {
    "bucket":     "test-bucket",
    "session_id": "sess-1",
    "run_id":     "run-abc",
    "program": {
        "therapeutic_area": "Oncology",
        "disease_type":     "Lung Cancer",
        "drug_name":        "Carboplatin",
    },
}

_MOCK_TEMPLATES = [
    {"module_key": "module2", "section_key": "2.5_clinical_overview", "content": "## Clinical Overview\n..."},
    {"module_key": "module5", "section_key": "5.3_clinical_study_reports", "content": "## Study Reports\n..."},
]

_MOCK_WRITER_RESPONSE = {
    "documents":       [],
    "sections_written": 2,
    "sections_failed":  [],
    "validation":      {"passed": True, "issues": [], "summary": "All sections valid."},
}


def _mock_no_existing_status(bucket_obj):
    """Configure GCS mock so no prior status file exists (first run)."""
    bucket_obj.blob.return_value.download_as_text.side_effect = Exception("not found")


# ── Tests: helpers ────────────────────────────────────────────────────────────

class TestProgramPrefix:
    def test_basic(self):
        assert _program_prefix("oncology", "lung_cancer", "carboplatin") == (
            "therapeutic-area/oncology/lung_cancer/carboplatin"
        )

    def test_slugging_spaces(self):
        assert _program_prefix("Oncology", "Lung Cancer", "Carboplatin") == (
            "therapeutic-area/oncology/lung_cancer/carboplatin"
        )

    def test_slugging_mixed(self):
        assert _program_prefix("  Neurology  ", "Bells Palsy", "Prednisolone") == (
            "therapeutic-area/neurology/bells_palsy/prednisolone"
        )


class TestOidcHeaders:
    def test_returns_empty_on_missing_credentials(self):
        with patch("google.oauth2.id_token.fetch_id_token", side_effect=Exception("no creds")):
            headers = _oidc_headers("https://example.run.app")
        assert headers == {}

    def test_returns_bearer_token(self):
        with patch("google.auth.transport.requests.Request"), \
             patch("google.oauth2.id_token.fetch_id_token", return_value="my-token"):
            result = _oidc_headers("https://example.run.app")
        assert result.get("Authorization") == "Bearer my-token"


# ── Tests: /health ────────────────────────────────────────────────────────────

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── Tests: /generate ─────────────────────────────────────────────────────────

class TestGenerateMalformed:
    def test_missing_message_key(self):
        resp = client.post("/generate", json={"not": "a pubsub envelope"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_invalid_base64(self):
        resp = client.post("/generate", json={"message": {"data": "!!!not-base64!!!"}})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_valid_base64_but_not_json(self):
        data = base64.b64encode(b"not json").decode()
        resp = client.post("/generate", json={"message": {"data": data}})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


class TestGenerateMissingFields:
    def test_missing_bucket(self):
        payload = {**_GOOD_PAYLOAD, "bucket": ""}
        resp = client.post("/generate", json=_pubsub_body(payload))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_missing_therapeutic_area(self):
        payload = {**_GOOD_PAYLOAD, "program": {**_GOOD_PAYLOAD["program"], "therapeutic_area": ""}}
        resp = client.post("/generate", json=_pubsub_body(payload))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


class TestGenerateDuplicate:
    def test_same_run_id_already_done(self):
        existing_status = json.dumps({"status": "done", "run_id": "run-abc"})
        mock_bkt = MagicMock()
        mock_bkt.blob.return_value.download_as_text.return_value = existing_status
        mock_bkt.blob.return_value.upload_from_string = MagicMock()
        _mock_gcs.bucket.return_value = mock_bkt

        resp = client.post("/generate", json=_pubsub_body(_GOOD_PAYLOAD))
        assert resp.status_code == 200
        assert resp.json()["status"] == "duplicate"


class TestGenerateHappyPath:
    def test_full_pipeline_success(self):
        mock_bkt = MagicMock()
        _mock_no_existing_status(mock_bkt)
        mock_bkt.blob.return_value.upload_from_string = MagicMock()
        _mock_gcs.bucket.return_value = mock_bkt

        orch_resp = MagicMock(status_code=200)
        orch_resp.json.return_value = {"templates": _MOCK_TEMPLATES, "modules_generated": 2}

        writer_resp = MagicMock(status_code=200)
        writer_resp.json.return_value = _MOCK_WRITER_RESPONSE

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.post = AsyncMock(side_effect=[orch_resp, writer_resp])

        with patch("worker_app._oidc_headers", return_value={}), \
             patch("httpx.AsyncClient", return_value=mock_async_client):
            resp = client.post("/generate", json=_pubsub_body(_GOOD_PAYLOAD))

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["sections_written"] == 2

    def test_templates_saved_to_gcs(self):
        """Verify each template is written to the correct GCS path."""
        mock_bkt = MagicMock()
        _mock_no_existing_status(mock_bkt)
        written_paths = []

        def capture_upload(content, content_type=None):
            pass

        def make_blob(path):
            b = MagicMock()
            b.upload_from_string.side_effect = lambda *a, **kw: written_paths.append(path)
            b.download_as_text.side_effect = Exception("not found")
            return b

        mock_bkt.blob.side_effect = make_blob
        _mock_gcs.bucket.return_value = mock_bkt

        orch_resp = MagicMock(status_code=200)
        orch_resp.json.return_value = {"templates": _MOCK_TEMPLATES, "modules_generated": 2}
        writer_resp = MagicMock(status_code=200)
        writer_resp.json.return_value = _MOCK_WRITER_RESPONSE

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.post = AsyncMock(side_effect=[orch_resp, writer_resp])

        with patch("worker_app._oidc_headers", return_value={}), \
             patch("httpx.AsyncClient", return_value=mock_async_client):
            client.post("/generate", json=_pubsub_body(_GOOD_PAYLOAD))

        # Check template paths follow the expected pattern
        template_writes = [p for p in written_paths if "/templates/" in p]
        assert any("module2/2.5_clinical_overview.md" in p for p in template_writes)
        assert any("module5/5.3_clinical_study_reports.md" in p for p in template_writes)


class TestGenerateOrchestratorErrors:
    def _run_with_orch_status(self, orch_status: int):
        mock_bkt = MagicMock()
        _mock_no_existing_status(mock_bkt)
        mock_bkt.blob.return_value.upload_from_string = MagicMock()
        _mock_gcs.bucket.return_value = mock_bkt

        orch_resp = MagicMock(status_code=orch_status)
        orch_resp.text = "error body"
        orch_resp.request = MagicMock()

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.post = AsyncMock(return_value=orch_resp)

        with patch("worker_app._oidc_headers", return_value={}), \
             patch("httpx.AsyncClient", return_value=mock_async_client):
            return client.post("/generate", json=_pubsub_body(_GOOD_PAYLOAD))

    def test_orchestrator_4xx_acks(self):
        resp = self._run_with_orch_status(422)
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    def test_orchestrator_5xx_raises(self):
        # 5xx from orchestrator should raise so Pub/Sub retries
        with pytest.raises(Exception):
            self._run_with_orch_status(503)


class TestGenerateWriterErrors:
    def _run_with_writer_status(self, writer_status: int):
        mock_bkt = MagicMock()
        _mock_no_existing_status(mock_bkt)
        mock_bkt.blob.return_value.upload_from_string = MagicMock()
        _mock_gcs.bucket.return_value = mock_bkt

        orch_resp = MagicMock(status_code=200)
        orch_resp.json.return_value = {"templates": _MOCK_TEMPLATES, "modules_generated": 2}

        writer_resp = MagicMock(status_code=writer_status)
        writer_resp.text = "writer error"
        writer_resp.request = MagicMock()

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.post = AsyncMock(side_effect=[orch_resp, writer_resp])

        with patch("worker_app._oidc_headers", return_value={}), \
             patch("httpx.AsyncClient", return_value=mock_async_client):
            return client.post("/generate", json=_pubsub_body(_GOOD_PAYLOAD))

    def test_writer_4xx_acks(self):
        resp = self._run_with_writer_status(422)
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    def test_writer_5xx_raises(self):
        # 5xx from writer should raise so Pub/Sub retries
        with pytest.raises(Exception):
            self._run_with_writer_status(503)


class TestGenerateTransientErrors:
    def test_connect_error_raises_for_retry(self):
        mock_bkt = MagicMock()
        _mock_no_existing_status(mock_bkt)
        mock_bkt.blob.return_value.upload_from_string = MagicMock()
        _mock_gcs.bucket.return_value = mock_bkt

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.post = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )

        with patch("worker_app._oidc_headers", return_value={}), \
             patch("httpx.AsyncClient", return_value=mock_async_client):
            with pytest.raises(httpx.ConnectError):
                client.post("/generate", json=_pubsub_body(_GOOD_PAYLOAD))

    def test_timeout_raises_for_retry(self):
        mock_bkt = MagicMock()
        _mock_no_existing_status(mock_bkt)
        mock_bkt.blob.return_value.upload_from_string = MagicMock()
        _mock_gcs.bucket.return_value = mock_bkt

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timed out")
        )

        with patch("worker_app._oidc_headers", return_value={}), \
             patch("httpx.AsyncClient", return_value=mock_async_client):
            with pytest.raises(httpx.TimeoutException):
                client.post("/generate", json=_pubsub_body(_GOOD_PAYLOAD))
