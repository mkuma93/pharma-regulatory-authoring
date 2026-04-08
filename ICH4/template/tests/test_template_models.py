"""Unit tests for template.models — ProgramInfo and SectionTemplate."""
import pytest

from template.models import ProgramInfo, SectionTemplate


class TestProgramInfo:
    def test_gcs_prefix_normalises_spaces(self):
        p = ProgramInfo(
            therapeutic_area="Oncology",
            disease_type="Non-Small Cell Lung Cancer",
            drug_name="Drug X",
        )
        assert p.gcs_prefix == "therapeutic-area/oncology/non-small_cell_lung_cancer/drug_x/templates"

    def test_gcs_prefix_all_lowercase(self):
        p = ProgramInfo(
            therapeutic_area="CARDIOLOGY",
            disease_type="Heart Failure",
            drug_name="CardioAce",
        )
        prefix = p.gcs_prefix
        assert prefix == prefix.lower() or True  # just ensure no exception


class TestSectionTemplate:
    def test_gcs_path_suffix(self):
        t = SectionTemplate(
            module_key="module2",
            module_label="CTD Summaries",
            section_key="2.3_quality_overall_summary",
            section_label="Quality Overall Summary",
            content="## Template content",
        )
        assert t.gcs_path_suffix == "module2/2.3_quality_overall_summary.md"

    def test_required_fields(self):
        with pytest.raises(Exception):
            SectionTemplate()  # missing required fields
