"""Tests for POST /schema-diff in clinical-analyst/app.py."""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Minimal env so the module imports cleanly without real GCP creds ──────────
os.environ.setdefault("OPENAI_API_KEY",  "test-key")
os.environ.setdefault("GCP_PROJECT_ID",  "test-project")
os.environ.setdefault("GCS_BUCKET",      "test-bucket")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_mock_gcs = MagicMock()
_mock_publisher = MagicMock()

with patch("google.cloud.storage.Client", return_value=_mock_gcs), \
     patch.dict("sys.modules", {"google.cloud.pubsub_v1": MagicMock()}):
    from app import app  # noqa: E402
    import app as _app_mod  # noqa: E402 — used to set _gcs singleton directly

client = TestClient(app, raise_server_exceptions=False)


# ── Shared manifest fixtures ──────────────────────────────────────────────────

def _manifest_with_keys(*placeholder_keys: str, sections: list[str] | None = None) -> dict:
    secs = sections or ["2.7.3", "5.3.5.1"]
    return {
        "program": {"therapeutic_area": "neurology", "disease_type": "bells_palsy", "drug_name": "prednisolone"},
        "sources": [
            {
                "filename": "trial.csv",
                "gcs_path": "gs://test-bucket/...",
                "study_type": "RCT",
                "column_mappings": [
                    {
                        "column_name": k,
                        "role": "efficacy_endpoint",
                        "ctd_section_keys": secs,
                        "placeholder_key": k,
                    }
                    for k in placeholder_keys
                ],
                "ctd_section_keys": secs,
            }
        ],
    }


_PROGRAM = {
    "therapeutic_area": "neurology",
    "disease_type":     "bells_palsy",
    "drug_name":        "prednisolone",
}


def _make_blob(data: bytes | None, exists: bool = True) -> MagicMock:
    b = MagicMock()
    b.exists.return_value = exists
    if data is not None:
        b.download_as_bytes.return_value = data
    else:
        b.download_as_bytes.side_effect = Exception("not found")
    return b


def _setup_bucket(mock_bkt: MagicMock) -> None:
    """Point the app's _gcs_client() singleton at mock_bkt."""
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bkt
    _app_mod._gcs = mock_client


# ── Tests: /schema-diff ───────────────────────────────────────────────────────

class TestSchemaDiffNoManifest:
    def test_returns_404_when_no_manifest(self):
        mock_bkt = MagicMock()
        mock_bkt.blob.return_value = _make_blob(None, exists=False)
        _setup_bucket(mock_bkt)

        resp = client.post("/schema-diff", json=_PROGRAM)
        assert resp.status_code == 404


class TestSchemaDiffNoSnapshot:
    def test_returns_no_changes_when_no_snapshot(self):
        current   = json.dumps(_manifest_with_keys("recovery_rate")).encode()
        snap_blob = _make_blob(None, exists=False)
        curr_blob = _make_blob(current, exists=True)

        def blob_factory(path: str):
            if "snapshot" in path:
                return snap_blob
            return curr_blob

        mock_bkt = MagicMock()
        mock_bkt.blob.side_effect = blob_factory
        _setup_bucket(mock_bkt)

        resp = client.post("/schema-diff", json=_PROGRAM)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_changes"] is False
        assert data["added_placeholders"]   == []
        assert data["changed_placeholders"] == []
        assert data["removed_placeholders"] == []


class TestSchemaDiffWithChanges:
    def _setup_blobs(self, current_manifest: dict, snapshot_manifest: dict):
        curr_blob = _make_blob(json.dumps(current_manifest).encode(),  exists=True)
        snap_blob = _make_blob(json.dumps(snapshot_manifest).encode(), exists=True)

        def blob_factory(path: str):
            if "snapshot" in path:
                return snap_blob
            return curr_blob

        mock_bkt = MagicMock()
        mock_bkt.blob.side_effect = blob_factory
        _setup_bucket(mock_bkt)

    def test_added_placeholder_detected(self):
        old = _manifest_with_keys("recovery_rate")
        new = _manifest_with_keys("recovery_rate", "adverse_events")
        self._setup_blobs(new, old)

        resp = client.post("/schema-diff", json=_PROGRAM)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_changes"] is True
        assert "adverse_events" in data["added_placeholders"]
        assert data["changed_placeholders"] == []
        assert data["removed_placeholders"] == []

    def test_removed_placeholder_detected(self):
        old = _manifest_with_keys("recovery_rate", "adverse_events")
        new = _manifest_with_keys("recovery_rate")
        self._setup_blobs(new, old)

        resp = client.post("/schema-diff", json=_PROGRAM)
        data = resp.json()
        assert data["has_changes"] is True
        assert "adverse_events" in data["removed_placeholders"]
        assert data["added_placeholders"] == []

    def test_changed_placeholder_detected_when_sections_differ(self):
        old_src = _manifest_with_keys("recovery_rate", sections=["2.7.3"])
        new_src = _manifest_with_keys("recovery_rate", sections=["2.7.3", "5.3.5.1"])
        self._setup_blobs(new_src, old_src)

        resp = client.post("/schema-diff", json=_PROGRAM)
        data = resp.json()
        assert data["has_changes"] is True
        assert "recovery_rate" in data["changed_placeholders"]

    def test_affected_sections_is_union_of_changed_keys(self):
        old = _manifest_with_keys("a", sections=["2.7.3"])
        new = _manifest_with_keys("a", "b", sections=["2.7.3"])
        # "b" is added — should include its section in affected_sections
        new["sources"][0]["column_mappings"][1]["ctd_section_keys"] = ["5.3.5.1"]
        new["sources"][0]["column_mappings"][1]["placeholder_key"]  = "b"
        self._setup_blobs(new, old)

        resp = client.post("/schema-diff", json=_PROGRAM)
        data = resp.json()
        assert "5.3.5.1" in data["affected_sections"]

    def test_no_changes_when_manifests_identical(self):
        manifest = _manifest_with_keys("recovery_rate")
        self._setup_blobs(manifest, manifest)

        resp = client.post("/schema-diff", json=_PROGRAM)
        data = resp.json()
        assert data["has_changes"] is False
        assert data["affected_sections"] == []


class TestSchemaDiffBucketFallback:
    def test_uses_default_bucket_when_not_supplied(self):
        current  = json.dumps(_manifest_with_keys("k")).encode()
        snap_blob = _make_blob(None, exists=False)
        curr_blob = _make_blob(current, exists=True)

        def blob_factory(path: str):
            return snap_blob if "snapshot" in path else curr_blob

        mock_bkt = MagicMock()
        mock_bkt.blob.side_effect = blob_factory
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bkt
        _app_mod._gcs = mock_client

        resp = client.post("/schema-diff", json=_PROGRAM)  # no "bucket" field
        assert resp.status_code == 200
        mock_client.bucket.assert_called_with("test-bucket")  # default env var
