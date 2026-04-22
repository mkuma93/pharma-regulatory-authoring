"""Unit tests for manifest snapshot logic in writer/clinical_uploader.py."""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock GCS + LangChain before importing the module under test
_mock_gcs_instance = MagicMock()
_mock_gcs_client   = MagicMock(return_value=_mock_gcs_instance)

with patch("google.cloud.storage.Client", _mock_gcs_client), \
     patch("langchain_openai.ChatOpenAI"):
    from writer.clinical_uploader import _snapshot_manifest  # noqa: E402
    # Patch the gcs() singleton used by _snapshot_manifest at the call site
    import writer.clinical_uploader as _uploader_mod
    import writer.gcs_client as _gcs_client_mod


# ── Fixtures ──────────────────────────────────────────────────────────────────

_MANIFEST = {
    "program": {"therapeutic_area": "neurology", "disease_type": "bells_palsy", "drug_name": "prednisolone"},
    "sources": [
        {
            "filename": "trial.csv",
            "gcs_path": "gs://bucket/therapeutic-area/neurology/bells_palsy/prednisolone/clinical_data/trial.csv",
            "study_type": "RCT",
            "column_mappings": [
                {"column_name": "recovery_rate", "role": "efficacy_endpoint",
                 "ctd_section_keys": ["2.7.3", "5.3.5.1"], "placeholder_key": "recovery_rate_3mo"},
            ],
            "ctd_section_keys": ["2.7.3", "5.3.5.1"],
        }
    ],
}

_MANIFEST_PATH = "therapeutic-area/neurology/bells_palsy/prednisolone/clinical_data/manifest.json"
_SNAP_PATH     = "therapeutic-area/neurology/bells_palsy/prednisolone/clinical_data/manifest_snapshot.json"


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSnapshotManifest:
    def _make_bucket(self, manifest_bytes: bytes | None, exists: bool = True):
        """Build a minimal GCS bucket mock."""
        mock_bucket = MagicMock()
        manifest_blob = MagicMock()
        snapshot_blob = MagicMock()

        manifest_blob.exists.return_value = exists
        manifest_blob.download_as_bytes.return_value = manifest_bytes or b""

        def blob_factory(path: str):
            return snapshot_blob if "snapshot" in path else manifest_blob

        mock_bucket.blob.side_effect = blob_factory
        return mock_bucket, snapshot_blob

    def _setup_gcs(self, bucket_mock: MagicMock) -> None:
        """Route the gcs() singleton to our bucket mock."""
        mock_client = MagicMock()
        mock_client.bucket.return_value = bucket_mock
        _gcs_client_mod._client = mock_client

    def test_snapshot_copied_when_manifest_exists(self):
        manifest_bytes = json.dumps(_MANIFEST).encode()
        mock_bkt, snap_blob = self._make_bucket(manifest_bytes, exists=True)
        self._setup_gcs(mock_bkt)

        _snapshot_manifest("my-bucket", _MANIFEST_PATH)

        snap_blob.upload_from_string.assert_called_once_with(
            manifest_bytes, content_type="application/json"
        )

    def test_no_snapshot_when_manifest_absent(self):
        mock_bkt, snap_blob = self._make_bucket(None, exists=False)
        self._setup_gcs(mock_bkt)

        _snapshot_manifest("my-bucket", _MANIFEST_PATH)

        snap_blob.upload_from_string.assert_not_called()

    def test_gcs_error_is_swallowed(self):
        """Snapshot failure must not raise — it is non-blocking."""
        mock_client = MagicMock()
        mock_client.bucket.side_effect = Exception("GCS exploded")
        _gcs_client_mod._client = mock_client

        # Must not raise
        _snapshot_manifest("my-bucket", _MANIFEST_PATH)

    def test_snapshot_path_derived_correctly(self):
        """Snapshot blob must use manifest_snapshot.json, not manifest.json."""
        manifest_bytes = json.dumps(_MANIFEST).encode()
        mock_bkt, snap_blob = self._make_bucket(manifest_bytes, exists=True)
        self._setup_gcs(mock_bkt)

        _snapshot_manifest("my-bucket", _MANIFEST_PATH)

        called_paths = [call.args[0] for call in mock_bkt.blob.call_args_list]
        assert any("manifest_snapshot.json" in p for p in called_paths)
