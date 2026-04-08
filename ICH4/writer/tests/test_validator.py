"""Unit tests for validator nodes — rule-based only, no LLM calls."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from writer.models import SectionDocument, ValidationIssue  # noqa: E402
from validator.state import ValidatorState  # noqa: E402
from validator.nodes import (  # noqa: E402
    extract_key_values,
    check_consistency,
    auto_fill_from_cross_sections,
    check_unfilled_placeholders,
    check_not_filled_markers,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _doc(section_key: str, content: str) -> SectionDocument:
    return SectionDocument(
        module_key="module2",
        module_label="CTD Summaries",
        section_key=section_key,
        section_label=section_key,
        content=content,
    )


def _state(docs: list[SectionDocument], **extras) -> ValidatorState:
    defaults = dict(
        issues=[],
        messages=[],
        extracted_values={},
        canonical_values={},
        drug_name_found="",
        passed=True,
        summary="",
    )
    defaults.update(extras)  # caller kwargs override defaults
    return ValidatorState(documents=docs, **defaults)


# ── extract_key_values ────────────────────────────────────────────────────────

class TestExtractKeyValues:
    def test_extracts_drug_name(self):
        state = _state([_doc("2.5", "The compound DrugX was administered at 100 mg.")])
        result = extract_key_values(state)
        assert result["canonical_values"].get("drug_name") == "DrugX"

    def test_extracts_study_n(self):
        state = _state([_doc("2.5", "A total of 245 patients were enrolled in the study.")])
        result = extract_key_values(state)
        assert result["canonical_values"].get("study_n") == "245"

    def test_extracts_study_n_from_N_equals(self):
        state = _state([_doc("2.7", "The ITT population was N=312 subjects.")])
        result = extract_key_values(state)
        assert result["canonical_values"].get("study_n") == "312"

    def test_extracts_mean_age(self):
        state = _state([_doc("2.5", "The mean age was 54.3 years at baseline.")])
        result = extract_key_values(state)
        assert result["canonical_values"].get("mean_age") == "54.3"

    def test_extracts_orr(self):
        state = _state([_doc("2.5", "The ORR was 68.4% (95% CI: 61–75%).")])
        result = extract_key_values(state)
        assert result["canonical_values"].get("efficacy_orr") == "68.4"

    def test_extracts_p_value(self):
        state = _state([_doc("2.5", "The primary endpoint was met (p < 0.001).")])
        result = extract_key_values(state)
        assert result["canonical_values"].get("p_value") == "0.001"

    def test_consensus_picks_most_common(self):
        docs = [
            _doc("2.5", "N=245 patients were enrolled."),
            _doc("2.7", "245 patients were enrolled."),
            _doc("2.6", "N=100 patients were enrolled."),  # outlier
        ]
        result = extract_key_values(_state(docs))
        assert result["canonical_values"]["study_n"] == "245"

    def test_empty_documents(self):
        result = extract_key_values(_state([]))
        assert result["canonical_values"] == {}
        assert result["extracted_values"] == {}


# ── check_consistency ─────────────────────────────────────────────────────────

class TestCheckConsistency:
    def test_no_issues_when_consistent(self):
        docs = [
            _doc("2.5", "compound DrugX was tested. N=245 patients."),
            _doc("2.7", "compound DrugX confirmed. N=245 subjects."),
        ]
        state = _state(docs)
        extracted = extract_key_values(state)
        state2 = state.model_copy(update=extracted)
        result = check_consistency(state2)
        assert result["issues"] == []

    def test_flags_drug_name_mismatch_as_error(self):
        extracted = {
            "extracted_values": {
                "2.5": {"drug_name": "DrugY"},  # differs from consensus
            },
            "canonical_values": {"drug_name": "DrugX"},
        }
        state = _state([_doc("2.5", "compound DrugY")], **extracted)
        result = check_consistency(state)
        errors = [i for i in result["issues"] if i.severity == "error"]
        assert len(errors) == 1
        assert "drug_name" in errors[0].message.lower() or "drug" in errors[0].message.lower()

    def test_flags_demographic_mismatch_as_warning(self):
        extracted = {
            "extracted_values": {"2.7": {"study_n": "100"}},
            "canonical_values": {"study_n": "245"},
        }
        state = _state([_doc("2.7", "100 patients were enrolled")], **extracted)
        result = check_consistency(state)
        warnings = [i for i in result["issues"] if i.severity == "warning"]
        assert len(warnings) >= 1
        assert "245" in warnings[0].message

    def test_flags_efficacy_mismatch_as_error(self):
        extracted = {
            "extracted_values": {"2.7": {"efficacy_orr": "45.0"}},
            "canonical_values": {"efficacy_orr": "68.4"},
        }
        state = _state([_doc("2.7", "ORR 45%")], **extracted)
        result = check_consistency(state)
        errors = [i for i in result["issues"] if i.severity == "error"]
        assert len(errors) >= 1

    def test_small_rounding_skip(self):
        # 68.4 vs 68.5 → <2% difference → should be skipped entirely
        extracted = {
            "extracted_values": {"2.7": {"efficacy_orr": "68.5"}},
            "canonical_values": {"efficacy_orr": "68.4"},
        }
        state = _state([_doc("2.7", "ORR 68.5%")], **extracted)
        result = check_consistency(state)
        assert result["issues"] == []

    def test_minor_rounding_info(self):
        # 68.4 vs 70.0 → 2–5% difference → info only
        extracted = {
            "extracted_values": {"2.7": {"efficacy_orr": "70.0"}},
            "canonical_values": {"efficacy_orr": "68.4"},
        }
        state = _state([_doc("2.7", "ORR 70%")], **extracted)
        result = check_consistency(state)
        infos = [i for i in result["issues"] if i.severity == "info"]
        assert len(infos) >= 1


# ── auto_fill_from_cross_sections ─────────────────────────────────────────────

class TestAutoFill:
    def test_fills_study_n(self):
        doc = _doc("2.7", "A total of [DATA PENDING — author to supply] patients were enrolled.")
        state = _state([doc], canonical_values={"study_n": "245"})
        result = auto_fill_from_cross_sections(state)
        filled_content = result["documents"][0].content
        assert "245" in filled_content
        assert "[DATA PENDING" not in filled_content

    def test_leaves_non_matching_pending(self):
        # Non-contextual DATA PENDING that doesn't match any fill pattern
        doc = _doc("2.7", "The formulation batch [DATA PENDING — author to supply] was used.")
        state = _state([doc], canonical_values={"study_n": "245"})
        result = auto_fill_from_cross_sections(state)
        # Should not be filled (no matching pattern)
        assert "[DATA PENDING" in result["documents"][0].content

    def test_no_canonical_values_leaves_docs_unchanged(self):
        doc = _doc("2.5", "N=245. All good. No pending.")
        state = _state([doc], canonical_values={})
        result = auto_fill_from_cross_sections(state)
        assert result["documents"][0].content == doc.content

    def test_adds_info_issue_on_fill(self):
        doc = _doc("2.5", "N=[DATA PENDING — author to supply].")
        state = _state([doc], canonical_values={"study_n": "312"})
        result = auto_fill_from_cross_sections(state)
        infos = [i for i in result["issues"] if i.severity == "info"]
        assert len(infos) == 1
        assert "Auto-filled" in infos[0].message

    def test_no_issues_when_no_pending(self):
        doc = _doc("2.5", "N=245 patients were enrolled.")
        state = _state([doc], canonical_values={"study_n": "245"})
        result = auto_fill_from_cross_sections(state)
        assert result["issues"] == []


# ── check_unfilled_placeholders ───────────────────────────────────────────────

class TestCheckUnfilled:
    def test_clean_content_passes(self):
        state = _state([_doc("2.5", "No markers here.")])
        result = check_unfilled_placeholders(state)
        assert result["issues"] == []

    def test_detects_data_pending(self):
        state = _state([_doc("2.5", "Rate: [DATA PENDING — author to supply].")])
        result = check_unfilled_placeholders(state)
        assert result["issues"][0].severity == "error"
        assert result["issues"][0].section_key == "2.5"

    def test_counts_multiple(self):
        state = _state([_doc("2.7", "[DATA PENDING] first. [DATA PENDING] second.")])
        result = check_unfilled_placeholders(state)
        assert "2" in result["issues"][0].message


# ── check_not_filled_markers ──────────────────────────────────────────────────

class TestCheckNotFilled:
    def test_clean_passes(self):
        state = _state([_doc("2.5", "All good.")])
        result = check_not_filled_markers(state)
        assert result["issues"] == []

    def test_detects_not_filled(self):
        state = _state([_doc("2.5", "Dose: {{dose}} [NOT FILLED].")])
        result = check_not_filled_markers(state)
        assert result["issues"][0].severity == "error"
        assert "{{dose}}" in result["issues"][0].message

