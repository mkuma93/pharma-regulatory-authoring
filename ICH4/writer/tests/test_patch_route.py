"""Integration tests for POST /patch in writer service.

Validates the /patch route:
  - Routes to write_section with prior_content when existing document found
  - Falls back to full write when no prior document exists
  - Filters by requested sections
  - Returns 404 when no templates found
  - Records sections_failed correctly
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GCP_PROJECT_ID", "")
os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sys as _sys

_mock_gcs = MagicMock()

# validator.nodes is missing check_against_resolved — stub the whole package
# before any writer import so the broken import chain never runs.
_mock_validator_graph = MagicMock()
_mock_validator_graph.run_validator = MagicMock()
for _key, _val in [
    ("validator",              MagicMock()),
    ("validator.graph",        _mock_validator_graph),
    ("validator.nodes",        MagicMock()),
    ("validator.state",        MagicMock()),
]:
    _sys.modules.setdefault(_key, _val)

with patch("google.cloud.storage.Client", return_value=_mock_gcs), \
     patch("langchain_openai.ChatOpenAI"):
    from api.app import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


# ── Shared fixtures ───────────────────────────────────────────────────────────

_PROGRAM = {
    "therapeutic_area": "neurology",
    "disease_type":     "bells_palsy",
    "drug_name":        "prednisolone",
}

_TEMPLATE_PATHS = [
    "therapeutic-area/neurology/bells_palsy/prednisolone/templates/module2/2.7.3_summary_efficacy.md",
    "therapeutic-area/neurology/bells_palsy/prednisolone/templates/module5/5.3.5.1_csr.md",
]

_EXISTING_DOC = "## Summary of Clinical Efficacy\n\nRecovery rate was 60%."
_FILLED_DOC   = "## Summary of Clinical Efficacy\n\nRecovery rate is now 72%."

_MOCK_SECTION_DOC = {
    "module_key":    "module2",
    "module_label":  "Module 2",
    "section_key":   "2.7.3_summary_efficacy",
    "section_label": "Summary Efficacy",
    "content":       _FILLED_DOC,
    "gcs_path":      "",
}


def _patch_route_deps(
    template_paths: list[str] = None,
    existing_doc:   str | None = _EXISTING_DOC,
    write_section_result = None,
):
    """Return a set of context managers that mock all I/O for /patch."""
    from writer.models import SectionDocument, ValidationResult, WriterResponse

    template_paths = template_paths or _TEMPLATE_PATHS

    mock_doc = SectionDocument(**_MOCK_SECTION_DOC)
    mock_doc.gcs_path = "gs://test-bucket/..."

    if write_section_result is None:
        write_section_result = mock_doc

    return (
        patch("api.routes.patch.list_template_paths",   return_value=template_paths),
        patch("api.routes.patch.load_template",         return_value="## Template {{placeholder}}"),
        patch("api.routes.patch._load_existing_document", return_value=existing_doc),
        patch("api.routes.patch.save_document",         return_value="gs://test-bucket/..."),
        patch("api.routes.patch.write_section",         return_value=write_section_result),
        patch("api.routes.patch.run_validator",
              return_value=ValidationResult(passed=True, issues=[], summary="ok")),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPatchRouteBasic:
    def test_returns_200(self):
        patches = _patch_route_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            resp = client.post("/patch", json={
                "program":       _PROGRAM,
                "bucket_name":   "test-bucket",
                "changed_keys":  ["recovery_rate_3mo"],
            })
        assert resp.status_code == 200

    def test_sections_written_matches_templates(self):
        patches = _patch_route_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            resp = client.post("/patch", json={
                "program":      _PROGRAM,
                "bucket_name":  "test-bucket",
                "changed_keys": ["recovery_rate_3mo"],
            })
        data = resp.json()
        assert data["sections_written"] == len(_TEMPLATE_PATHS)
        assert data["sections_failed"]  == []

    def test_no_templates_returns_404(self):
        with patch("api.routes.patch.list_template_paths", return_value=[]):
            resp = client.post("/patch", json={
                "program":     _PROGRAM,
                "bucket_name": "test-bucket",
            })
        assert resp.status_code == 404


class TestPatchRoutePriorContent:
    def test_calls_write_section_with_prior_content(self):
        from writer.models import SectionDocument
        mock_doc = SectionDocument(**_MOCK_SECTION_DOC)
        write_mock = MagicMock(return_value=mock_doc)

        with patch("api.routes.patch.list_template_paths",    return_value=[_TEMPLATE_PATHS[0]]), \
             patch("api.routes.patch.load_template",          return_value="## Tmpl"), \
             patch("api.routes.patch._load_existing_document", return_value=_EXISTING_DOC), \
             patch("api.routes.patch.save_document",          return_value="gs://..."), \
             patch("api.routes.patch.write_section",          write_mock), \
             patch("api.routes.patch.run_validator",
                   return_value=MagicMock(passed=True, issues=[], summary="ok")):

            client.post("/patch", json={
                "program":      _PROGRAM,
                "bucket_name":  "test-bucket",
                "changed_keys": ["recovery_rate_3mo"],
            })

        call_kwargs = write_mock.call_args.kwargs
        assert call_kwargs["prior_content"] == _EXISTING_DOC
        assert "recovery_rate_3mo" in call_kwargs["changed_keys"]

    def test_falls_back_to_full_write_when_no_prior_doc(self):
        from writer.models import SectionDocument
        mock_doc = SectionDocument(**_MOCK_SECTION_DOC)
        write_mock = MagicMock(return_value=mock_doc)

        with patch("api.routes.patch.list_template_paths",     return_value=[_TEMPLATE_PATHS[0]]), \
             patch("api.routes.patch.load_template",           return_value="## Tmpl"), \
             patch("api.routes.patch._load_existing_document", return_value=None), \
             patch("api.routes.patch.save_document",           return_value="gs://..."), \
             patch("api.routes.patch.write_section",           write_mock), \
             patch("api.routes.patch.run_validator",
                   return_value=MagicMock(passed=True, issues=[], summary="ok")):

            client.post("/patch", json={
                "program":     _PROGRAM,
                "bucket_name": "test-bucket",
            })

        call_kwargs = write_mock.call_args.kwargs
        # prior_content=None means full-write fallback
        assert call_kwargs["prior_content"] is None


class TestPatchRouteSectionFilter:
    def test_filters_to_requested_sections(self):
        from writer.models import SectionDocument
        mock_doc = SectionDocument(**_MOCK_SECTION_DOC)
        write_mock = MagicMock(return_value=mock_doc)

        with patch("api.routes.patch.list_template_paths",     return_value=_TEMPLATE_PATHS), \
             patch("api.routes.patch.load_template",           return_value="## Tmpl"), \
             patch("api.routes.patch._load_existing_document", return_value=None), \
             patch("api.routes.patch.save_document",           return_value="gs://..."), \
             patch("api.routes.patch.write_section",           write_mock), \
             patch("api.routes.patch.run_validator",
                   return_value=MagicMock(passed=True, issues=[], summary="ok")):

            resp = client.post("/patch", json={
                "program":     _PROGRAM,
                "bucket_name": "test-bucket",
                # request only one of the two available sections
                "sections":    ["2.7.3_summary_efficacy"],
            })

        # write_section must be called exactly once (the filtered section)
        assert write_mock.call_count == 1
        assert resp.json()["sections_written"] == 1


class TestPatchRouteFailedSection:
    def test_failed_section_recorded(self):
        with patch("api.routes.patch.list_template_paths",     return_value=[_TEMPLATE_PATHS[0]]), \
             patch("api.routes.patch.load_template",           return_value="## Tmpl"), \
             patch("api.routes.patch._load_existing_document", return_value=None), \
             patch("api.routes.patch.save_document",           return_value="gs://..."), \
             patch("api.routes.patch.write_section",           side_effect=RuntimeError("LLM burst")), \
             patch("api.routes.patch.run_validator",
                   return_value=MagicMock(passed=True, issues=[], summary="ok")):

            resp = client.post("/patch", json={
                "program":     _PROGRAM,
                "bucket_name": "test-bucket",
            })

        data = resp.json()
        assert resp.status_code == 200
        assert data["sections_written"] == 0
        assert len(data["sections_failed"]) == 1
