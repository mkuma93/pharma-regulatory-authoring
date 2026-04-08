"""Unit tests for clinical.models — ClinicalDataManifest and helpers."""
import pytest

from template.models import ProgramInfo
from clinical.models import (
    ColumnMapping,
    ClinicalDataManifest,
    ClinicalDataSource,
)


_PROGRAM = ProgramInfo(
    therapeutic_area="oncology",
    disease_type="lung cancer",
    drug_name="testdrug",
)


class TestColumnMapping:
    def test_valid_role(self):
        cm = ColumnMapping(
            column_name="OS_MONTHS",
            role="efficacy_endpoint",
            ctd_section_keys=["2.7.3"],
            placeholder_key="os_months",
        )
        assert cm.role == "efficacy_endpoint"

    def test_invalid_role_raises(self):
        with pytest.raises(Exception):
            ColumnMapping(
                column_name="X",
                role="invalid_role",
                ctd_section_keys=[],
                placeholder_key="x",
            )


class TestClinicalDataManifest:
    def test_empty_sources_defaults(self):
        m = ClinicalDataManifest(program=_PROGRAM)
        assert m.sources == []
        assert m.last_updated == ""

    def test_roundtrip_json(self):
        source = ClinicalDataSource(
            filename="trial.csv",
            gcs_path="gs://bucket/trial.csv",
            study_type="RCT",
        )
        m = ClinicalDataManifest(program=_PROGRAM, sources=[source])
        restored = ClinicalDataManifest.model_validate_json(m.model_dump_json())
        assert restored.sources[0].filename == "trial.csv"
        assert restored.program.drug_name == "testdrug"
