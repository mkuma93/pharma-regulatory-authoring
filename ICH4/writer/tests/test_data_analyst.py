"""Tests for writer/data_analyst.py — deterministic tool functions only.

The LLM dispatcher (analyse_dataframe) is NOT tested here — it requires
a live API key and is covered by integration tests.  The four statistical
tools are pure pandas operations and can be tested without any mocking.
"""
from __future__ import annotations

import json
import pytest

# The tools are LangChain @tool objects; invoke them via .invoke(args_dict)
import pandas as pd

from writer.data_analyst import (
    _detect_group_column,
    compute_proportion,
    compute_mean_sd,
    compute_crosstab,
    compute_median_range,
)

# ── Shared Bell's Palsy fixture ───────────────────────────────────────────────

_PALSY_ROWS = [
    {"Treatment Group": "Prednisolone–Placebo", "Full Recovery in 3 Months": "Yes", "3-Month Score on House\u2013Brackmann scale": "1", "Age": "61"},
    {"Treatment Group": "Prednisolone–Placebo", "Full Recovery in 3 Months": "Yes", "3-Month Score on House\u2013Brackmann scale": "1", "Age": "55"},
    {"Treatment Group": "Prednisolone–Placebo", "Full Recovery in 3 Months": "No",  "3-Month Score on House\u2013Brackmann scale": "3", "Age": "77"},
    {"Treatment Group": "Prednisolone–Placebo", "Full Recovery in 3 Months": "No",  "3-Month Score on House\u2013Brackmann scale": "4", "Age": "45"},
    {"Treatment Group": "Placebo–Placebo",       "Full Recovery in 3 Months": "Yes", "3-Month Score on House\u2013Brackmann scale": "1", "Age": "60"},
    {"Treatment Group": "Placebo–Placebo",       "Full Recovery in 3 Months": "No",  "3-Month Score on House\u2013Brackmann scale": "5", "Age": "70"},
    {"Treatment Group": "Placebo–Placebo",       "Full Recovery in 3 Months": "No",  "3-Month Score on House\u2013Brackmann scale": "6", "Age": "40"},
    {"Treatment Group": "Placebo–Placebo",       "Full Recovery in 3 Months": "No",  "3-Month Score on House\u2013Brackmann scale": "6", "Age": "50"},
]
_DATA_JSON = json.dumps(_PALSY_ROWS)


# ── compute_proportion ────────────────────────────────────────────────────────

class TestComputeProportion:
    def test_overall_proportion(self):
        result = compute_proportion.invoke({
            "data_json": _DATA_JSON,
            "column": "Full Recovery in 3 Months",
            "positive_value": "Yes",
            "group_by": "",
        })
        # 3 Yes out of 8 total = 37.5%
        assert "37.5%" in result
        assert "(3/8)" in result

    def test_proportion_by_group(self):
        result = compute_proportion.invoke({
            "data_json": _DATA_JSON,
            "column": "Full Recovery in 3 Months",
            "positive_value": "Yes",
            "group_by": "Treatment Group",
        })
        # Prednisolone group: 2/4 = 50.0%
        assert "50.0%" in result
        # Placebo group: 1/4 = 25.0%
        assert "25.0%" in result

    def test_missing_column_returns_message(self):
        result = compute_proportion.invoke({
            "data_json": _DATA_JSON,
            "column": "Nonexistent Column",
            "positive_value": "Yes",
            "group_by": "",
        })
        assert "not found" in result


# ── compute_mean_sd ───────────────────────────────────────────────────────────

class TestComputeMeanSd:
    def test_overall_mean(self):
        result = compute_mean_sd.invoke({
            "data_json": _DATA_JSON,
            "column": "Age",
            "group_by": "",
        })
        # Ages: 61, 55, 77, 45, 60, 70, 40, 50 → mean = 57.25
        assert "57.25" in result
        assert "n=8" in result

    def test_mean_by_group(self):
        result = compute_mean_sd.invoke({
            "data_json": _DATA_JSON,
            "column": "Age",
            "group_by": "Treatment Group",
        })
        assert "Prednisolone–Placebo" in result
        assert "Placebo–Placebo" in result

    def test_non_numeric_column_returns_nan_gracefully(self):
        result = compute_mean_sd.invoke({
            "data_json": _DATA_JSON,
            "column": "Full Recovery in 3 Months",
            "group_by": "",
        })
        # All values become NaN after pd.to_numeric — result should still return
        assert isinstance(result, str)

    def test_missing_column(self):
        result = compute_mean_sd.invoke({
            "data_json": _DATA_JSON,
            "column": "Missing",
            "group_by": "",
        })
        assert "not found" in result


# ── compute_crosstab ──────────────────────────────────────────────────────────

class TestComputeCrosstab:
    def test_basic_crosstab(self):
        result = compute_crosstab.invoke({
            "data_json": _DATA_JSON,
            "row_column": "Treatment Group",
            "col_column": "Full Recovery in 3 Months",
        })
        # Should produce a Markdown table
        assert "|" in result
        assert "Yes" in result
        assert "No" in result
        assert "Prednisolone" in result

    def test_missing_column(self):
        result = compute_crosstab.invoke({
            "data_json": _DATA_JSON,
            "row_column": "Treatment Group",
            "col_column": "NonExistent",
        })
        assert "not found" in result


# ── compute_median_range ──────────────────────────────────────────────────────

class TestComputeMedianRange:
    def test_overall_median(self):
        result = compute_median_range.invoke({
            "data_json": _DATA_JSON,
            "column": "Age",
            "group_by": "",
        })
        # Ages sorted: 40, 45, 50, 55, 60, 61, 70, 77 → median = (55+60)/2 = 57.5
        assert "57.5" in result
        assert "40" in result   # min
        assert "77" in result   # max
        assert "n=8" in result

    def test_median_by_group(self):
        result = compute_median_range.invoke({
            "data_json": _DATA_JSON,
            "column": "Age",
            "group_by": "Treatment Group",
        })
        assert "Prednisolone–Placebo" in result
        assert "Placebo–Placebo" in result

    def test_missing_column(self):
        result = compute_median_range.invoke({
            "data_json": _DATA_JSON,
            "column": "Ghost",
            "group_by": "",
        })
        assert "not found" in result


# ── _detect_group_column — drug-agnostic column detection ────────────────────

class TestDetectGroupColumn:
    def _df(self, cols):
        return pd.DataFrame(columns=cols)

    def test_detects_treatment_group(self):
        assert _detect_group_column(self._df(["Treatment Group", "Age", "Outcome"])) == "Treatment Group"

    def test_detects_arm(self):
        assert _detect_group_column(self._df(["Subject ID", "Arm", "Response"])) == "Arm"

    def test_detects_study_arm(self):
        assert _detect_group_column(self._df(["Subject", "Study Arm", "AE"])) == "Study Arm"

    def test_detects_intervention(self):
        assert _detect_group_column(self._df(["ID", "Intervention", "Score"])) == "Intervention"

    def test_detects_cohort(self):
        assert _detect_group_column(self._df(["Cohort", "DoseLevel", "PFS"])) == "Cohort"

    def test_detects_randomization(self):
        assert _detect_group_column(self._df(["PatientID", "Randomization Group", "Weeks"])) == "Randomization Group"

    def test_no_group_returns_empty(self):
        assert _detect_group_column(self._df(["PatientID", "Age", "Weight", "Outcome"])) == ""
