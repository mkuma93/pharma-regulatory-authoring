"""
Tests for api/gcs.py — download_index_from_gcs.

Settings are owned by the test: each test constructs a real Settings()
instance with the values it needs (bucket, prefix, persist_dir), then uses
patch() as a context manager to swap it in for the duration of that one test.
The app module is never permanently modified; patch reverts on exit.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.gcs import download_index_from_gcs
from config.settings import Settings


def _settings(*, bucket: str, prefix: str = "index_store", persist_dir: Path) -> Settings:
    """Create a real Settings instance with the given GCS values.

    Required API keys are satisfied by the env vars set in conftest.py.
    All other fields keep their defaults.
    """
    return Settings(
        gcs_bucket_name=bucket,
        gcs_index_prefix=prefix,
        index_persist_dir=persist_dir,
    )


def _make_blob(name: str) -> MagicMock:
    blob = MagicMock()
    blob.name = name
    return blob


class TestDownloadIndexFromGcs:
    def test_downloads_blobs_to_local_dir(self, tmp_path):
        s = _settings(bucket="test-bucket", persist_dir=tmp_path / "index")
        blobs = [
            _make_blob("index_store/docstore.json"),
            _make_blob("index_store/vector_store.json"),
        ]
        mock_client = MagicMock()
        mock_client.list_blobs.return_value = blobs

        with patch("api.gcs.settings", s), \
             patch("google.cloud.storage.Client", return_value=mock_client):
            download_index_from_gcs()

        blobs[0].download_to_filename.assert_called_once()
        blobs[1].download_to_filename.assert_called_once()

    def test_creates_local_directory_if_missing(self, tmp_path):
        dest = tmp_path / "brand_new_dir"
        assert not dest.exists()

        s = _settings(bucket="test-bucket", persist_dir=dest)
        mock_client = MagicMock()
        mock_client.list_blobs.return_value = [_make_blob("index_store/docstore.json")]

        with patch("api.gcs.settings", s), \
             patch("google.cloud.storage.Client", return_value=mock_client):
            download_index_from_gcs()

        assert dest.exists()

    def test_raises_when_no_blobs_found(self, tmp_path):
        s = _settings(bucket="empty-bucket", persist_dir=tmp_path)
        mock_client = MagicMock()
        mock_client.list_blobs.return_value = []

        with patch("api.gcs.settings", s), \
             patch("google.cloud.storage.Client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="No index files found"):
                download_index_from_gcs()

    def test_skips_blobs_with_empty_relative_path(self, tmp_path):
        """Blob whose name equals the prefix exactly (a folder marker) is skipped."""
        s = _settings(bucket="test-bucket", persist_dir=tmp_path / "index")
        folder_marker = _make_blob("index_store/")   # relative portion is ""
        real_blob = _make_blob("index_store/docstore.json")
        mock_client = MagicMock()
        mock_client.list_blobs.return_value = [folder_marker, real_blob]

        with patch("api.gcs.settings", s), \
             patch("google.cloud.storage.Client", return_value=mock_client):
            download_index_from_gcs()

        folder_marker.download_to_filename.assert_not_called()
        real_blob.download_to_filename.assert_called_once()

    def test_passes_correct_bucket_and_prefix_to_client(self, tmp_path):
        s = _settings(bucket="my-specific-bucket", prefix="custom/prefix", persist_dir=tmp_path)
        mock_client = MagicMock()
        mock_client.list_blobs.return_value = [_make_blob("custom/prefix/file.json")]

        with patch("api.gcs.settings", s), \
             patch("google.cloud.storage.Client", return_value=mock_client):
            download_index_from_gcs()

        mock_client.list_blobs.assert_called_once_with(
            "my-specific-bucket", prefix="custom/prefix/"
        )
