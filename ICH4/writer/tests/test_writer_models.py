"""Unit tests for writer Pydantic models."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from writer.models import (  # noqa: E402
    ProgramInfo,
    SectionDocument,
    ValidationIssue,
    ValidationResult,
    WriterRequest,
    WriterResponse,
)


class TestProgramInfo:
    def test_fields(self):
        p = ProgramInfo(therapeutic_area="oncology", disease_type="nsclc", drug_name="drugx")
        assert p.therapeutic_area == "oncology"
        assert p.disease_type == "nsclc"
        assert p.drug_name == "drugx"

    def test_required_fields(self):
        with pytest.raises(Exception):
            ProgramInfo(therapeutic_area="ta", disease_type="dis")  # missing drug_name


class TestValidationIssue:
    def test_severity_literals(self):
        for sev in ("error", "warning", "info"):
            issue = ValidationIssue(severity=sev, section_key="2.5", message="msg")
            assert issue.severity == sev

    def test_invalid_severity(self):
        with pytest.raises(Exception):
            ValidationIssue(severity="critical", section_key="2.5", message="msg")


class TestValidationResult:
    def test_defaults(self):
        r = ValidationResult(passed=True, issues=[], summary="ok")
        assert r.passed is True

    def test_with_issues(self):
        issue = ValidationIssue(severity="error", section_key="2.7", message="bad")
        r = ValidationResult(passed=False, issues=[issue], summary="failed")
        assert len(r.issues) == 1
        assert r.issues[0].severity == "error"


class TestWriterRequest:
    def test_defaults(self):
        r = WriterRequest(
            program=ProgramInfo(therapeutic_area="ta", disease_type="dis", drug_name="drug"),
            bucket_name="my-bucket",
        )
        assert r.sections == []
        assert r.run_validator is True

    def test_selective_sections(self):
        r = WriterRequest(
            program=ProgramInfo(therapeutic_area="ta", disease_type="dis", drug_name="drug"),
            bucket_name="my-bucket",
            sections=["2.5_clinical_overview"],
        )
        assert "2.5_clinical_overview" in r.sections


class TestWriterResponse:
    def test_structure(self):
        doc = SectionDocument(
            module_key="module2",
            module_label="CTD Summaries",
            section_key="2.5",
            section_label="Clinical Overview",
            content="Content here.",
        )
        result = ValidationResult(passed=True, issues=[], summary="ok")
        resp = WriterResponse(
            documents=[doc],
            validation=result,
            sections_written=1,
            sections_failed=[],
        )
        assert resp.sections_written == 1
        assert resp.sections_failed == []
        assert resp.validation.passed is True
