"""Unit tests for orchestrator Pydantic models."""
import pytest

from models import OrchestratorRequest, OrchestratorResponse, ProgramInfo, SectionTemplate


_PROGRAM = ProgramInfo(
    therapeutic_area="oncology",
    disease_type="lung cancer",
    drug_name="testdrug",
)


class TestOrchestratorRequest:
    def test_defaults(self):
        req = OrchestratorRequest(program=_PROGRAM)
        assert req.include_clinical_data is True
        assert req.include_ich_context is True

    def test_explicit_flags(self):
        req = OrchestratorRequest(
            program=_PROGRAM,
            include_clinical_data=False,
            include_ich_context=False,
        )
        assert req.include_clinical_data is False
        assert req.include_ich_context is False

    def test_missing_program_raises(self):
        with pytest.raises(Exception):
            OrchestratorRequest()


class TestOrchestratorResponse:
    def _make_response(self, templates=None):
        return OrchestratorResponse(
            templates=templates or [],
            modules_generated=[],
            ich_index_used=False,
            clinical_data_used=False,
        )

    def test_empty_templates(self):
        resp = self._make_response()
        assert resp.templates == []

    def test_with_templates(self):
        t = SectionTemplate(
            module_key="module2",
            module_label="CTD Summaries",
            section_key="2.3_quality_overall_summary",
            section_label="Quality Overall Summary",
            content="## QOS Template",
        )
        resp = self._make_response(templates=[t])
        assert len(resp.templates) == 1
        assert resp.templates[0].module_key == "module2"
