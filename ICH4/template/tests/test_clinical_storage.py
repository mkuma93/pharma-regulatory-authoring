"""Unit tests for clinical.storage — path logic (GCS calls mocked)."""
from unittest.mock import MagicMock, patch

import pytest

from template.models import ProgramInfo
from clinical.models import ClinicalDataManifest
from clinical import storage as cs


_PROGRAM = ProgramInfo(
    therapeutic_area="Oncology",
    disease_type="Lung Cancer",
    drug_name="TestDrug",
)

_EXPECTED_PATH = "therapeutic-area/oncology/lung_cancer/testdrug/clinical_data/manifest.json"


def test_manifest_path():
    assert cs._manifest_path(_PROGRAM) == _EXPECTED_PATH


def test_load_manifest_returns_none_when_blob_missing():
    mock_blob = MagicMock()
    mock_blob.exists.return_value = False

    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    with patch.object(cs, "_gcs", return_value=mock_client):
        result = cs.load_manifest("test-bucket", _PROGRAM)

    assert result is None


def test_load_manifest_returns_manifest_when_blob_exists():
    manifest = ClinicalDataManifest(program=_PROGRAM)

    mock_blob = MagicMock()
    mock_blob.exists.return_value = True
    mock_blob.download_as_text.return_value = manifest.model_dump_json()

    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    with patch.object(cs, "_gcs", return_value=mock_client):
        result = cs.load_manifest("test-bucket", _PROGRAM)

    assert result is not None
    assert result.program.drug_name == "TestDrug"


def test_save_manifest_uploads_to_correct_path():
    manifest = ClinicalDataManifest(program=_PROGRAM)

    mock_blob = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    with patch.object(cs, "_gcs", return_value=mock_client):
        path = cs.save_manifest("test-bucket", manifest)

    assert path == _EXPECTED_PATH
    mock_bucket.blob.assert_called_once_with(_EXPECTED_PATH)
    mock_blob.upload_from_string.assert_called_once()
