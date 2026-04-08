"""
Tests for ctd_structure/scaffold.py

Covers:
  - scaffold_template: creates folders, skips existing, returns template path
  - copy_to_program: copies template to destination, skips if present, raises when missing
  - scaffold_locally: creates folder hierarchy under a base dir
  - scaffold_in_gcs: uploads .keep blobs + updates status.json in a GCS bucket

Testing approach:
  - _TEMPLATE_DIR is re-pointed at a tmp_path via monkeypatch so the real
    package directory is never written to. monkeypatch reverts after each test.
  - GCS bucket is the only external boundary; it is replaced with a MagicMock
    passed directly as an argument (scaffold_in_gcs takes the bucket as a
    parameter, not from a settings singleton, so no app code is modified).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from ctd_structure.scaffold import (
    copy_to_program,
    scaffold_in_gcs,
    scaffold_locally,
    scaffold_template,
)


# ── scaffold_template ─────────────────────────────────────────────────────────

class TestScaffoldTemplate:
    def test_creates_folders(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ctd_structure.scaffold._TEMPLATE_DIR", tmp_path / "ctd")
        scaffold_template(["ctd/module1/", "ctd/module1/1.1_toc/"])
        assert (tmp_path / "ctd" / "module1").is_dir()
        assert (tmp_path / "ctd" / "module1" / "1.1_toc").is_dir()

    def test_returns_template_dir_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ctd_structure.scaffold._TEMPLATE_DIR", tmp_path / "ctd")
        result = scaffold_template(["ctd/module1/"])
        assert result == tmp_path / "ctd"

    def test_skips_existing_folders(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ctd_structure.scaffold._TEMPLATE_DIR", tmp_path / "ctd")
        (tmp_path / "ctd" / "module1").mkdir(parents=True)
        scaffold_template(["ctd/module1/"])  # must not raise
        assert (tmp_path / "ctd" / "module1").is_dir()

    def test_empty_folder_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ctd_structure.scaffold._TEMPLATE_DIR", tmp_path / "ctd")
        result = scaffold_template([])
        assert result == tmp_path / "ctd"

    def test_creates_nested_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ctd_structure.scaffold._TEMPLATE_DIR", tmp_path / "ctd")
        scaffold_template(["ctd/module2/2.5_clinical/2.5.1_rationale/"])
        assert (tmp_path / "ctd" / "module2" / "2.5_clinical" / "2.5.1_rationale").is_dir()


# ── copy_to_program ────────────────────────────────────────────────────────────

class TestCopyToProgram:
    def test_copies_template_to_destination(self, tmp_path, monkeypatch):
        template_dir = tmp_path / "tpl" / "ctd"
        template_dir.mkdir(parents=True)
        (template_dir / "module1").mkdir()
        monkeypatch.setattr("ctd_structure.scaffold._TEMPLATE_DIR", template_dir)

        dest = tmp_path / "programs" / "neurology" / "bells_palsy" / "pred"
        copy_to_program(dest)

        assert (dest / "ctd").is_dir()
        assert (dest / "ctd" / "module1").is_dir()

    def test_raises_when_template_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "ctd_structure.scaffold._TEMPLATE_DIR",
            tmp_path / "does_not_exist" / "ctd",
        )
        with pytest.raises(FileNotFoundError, match="CTD template not found"):
            copy_to_program(tmp_path / "dest")

    def test_skips_when_destination_already_exists(self, tmp_path, monkeypatch, capsys):
        template_dir = tmp_path / "tpl" / "ctd"
        template_dir.mkdir(parents=True)
        monkeypatch.setattr("ctd_structure.scaffold._TEMPLATE_DIR", template_dir)

        dest = tmp_path / "dest"
        (dest / "ctd").mkdir(parents=True)

        copy_to_program(dest)

        assert "skipped" in capsys.readouterr().out

    def test_deep_template_contents_copied(self, tmp_path, monkeypatch):
        template_dir = tmp_path / "tpl" / "ctd"
        deep = template_dir / "module2" / "2.5_clinical"
        deep.mkdir(parents=True)
        monkeypatch.setattr("ctd_structure.scaffold._TEMPLATE_DIR", template_dir)

        dest = tmp_path / "prog"
        copy_to_program(dest)

        assert (dest / "ctd" / "module2" / "2.5_clinical").is_dir()


# ── scaffold_locally ──────────────────────────────────────────────────────────

class TestScaffoldLocally:
    def test_creates_full_hierarchy(self, tmp_path):
        scaffold_locally(tmp_path, ["ctd/module1/", "ctd/module1/1.1_toc/", "ctd/module2/"])
        assert (tmp_path / "ctd" / "module1").is_dir()
        assert (tmp_path / "ctd" / "module1" / "1.1_toc").is_dir()
        assert (tmp_path / "ctd" / "module2").is_dir()

    def test_idempotent_on_existing_dirs(self, tmp_path):
        (tmp_path / "ctd" / "module1").mkdir(parents=True)
        scaffold_locally(tmp_path, ["ctd/module1/"])  # must not raise

    def test_empty_list_is_no_op(self, tmp_path):
        scaffold_locally(tmp_path, [])
        assert not (tmp_path / "ctd").exists()


# ── scaffold_in_gcs ───────────────────────────────────────────────────────────
#
# The bucket is injected as an argument, so tests pass a MagicMock bucket
# directly. No app module is patched; the mock only lives in the test scope.

def _make_bucket(name: str = "test-bucket") -> MagicMock:
    """Return a mock GCS Bucket whose blobs default to not-existing."""
    bucket = MagicMock()
    bucket.name = name
    blob = MagicMock()
    blob.exists.return_value = False
    bucket.blob.return_value = blob
    return bucket


class TestScaffoldInGcs:
    def test_uploads_keep_blobs_for_each_folder(self):
        bucket = _make_bucket()
        scaffold_in_gcs(bucket, "programs/neuro/bells/pred", ["ctd/module1/"])
        bucket.blob.assert_any_call("programs/neuro/bells/pred/ctd/module1/.keep")
        bucket.blob.return_value.upload_from_string.assert_called()

    def test_skips_existing_blobs(self, capsys):
        bucket = _make_bucket()
        bucket.blob.return_value.exists.return_value = True  # already exists

        scaffold_in_gcs(bucket, "programs/neuro/bells/pred", ["ctd/module1/"])

        # upload_from_string should only be called for the status.json update, not the .keep
        calls = bucket.blob.return_value.upload_from_string.call_args_list
        # All calls should be for status.json (content_type=application/json)
        for c in calls:
            assert c.kwargs.get("content_type") == "application/json"

        assert "exists" in capsys.readouterr().out

    def test_updates_status_json(self):
        bucket = _make_bucket()
        # Simulate existing status.json with prior data
        existing = json.dumps({"other_key": True})
        bucket.blob.return_value.download_as_text.return_value = existing

        scaffold_in_gcs(bucket, "base/path", ["ctd/module1/"])

        status_blob = bucket.blob("base/path/workflow/status.json")
        upload_call = status_blob.upload_from_string.call_args
        payload = json.loads(upload_call.args[0])
        assert payload["ctd_created"] is True
        assert payload["ctd_created_date"] == str(date.today())
        assert payload["other_key"] is True      # prior data preserved

    def test_status_json_written_even_with_empty_existing(self):
        bucket = _make_bucket()
        bucket.blob.return_value.download_as_text.side_effect = Exception("not found")

        scaffold_in_gcs(bucket, "base/path", ["ctd/module1/"])

        # status.json upload should still happen
        status_blob = bucket.blob("base/path/workflow/status.json")
        payload = json.loads(status_blob.upload_from_string.call_args.args[0])
        assert payload["ctd_created"] is True

    def test_multiple_folders_all_uploaded(self):
        bucket = _make_bucket()
        paths = ["ctd/module1/", "ctd/module1/1.1_toc/", "ctd/module2/"]

        scaffold_in_gcs(bucket, "prog/base", paths)

        blob_paths_created = [
            c.args[0] for c in bucket.blob.call_args_list
        ]
        assert "prog/base/ctd/module1/.keep" in blob_paths_created
        assert "prog/base/ctd/module1/1.1_toc/.keep" in blob_paths_created
        assert "prog/base/ctd/module2/.keep" in blob_paths_created
