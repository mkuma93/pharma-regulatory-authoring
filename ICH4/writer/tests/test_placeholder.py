"""Unit tests for writer/placeholder.py."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from writer.placeholder import extract_placeholders, fill_placeholders  # noqa: E402


class TestExtractPlaceholders:
    def test_single_placeholder(self):
        assert extract_placeholders("Hello {{drug_name}}!") == ["drug_name"]

    def test_multiple_unique(self):
        tmpl = "{{drug_name}} was studied in {{study_population}}."
        keys = extract_placeholders(tmpl)
        assert sorted(keys) == ["drug_name", "study_population"]

    def test_deduplicates(self):
        tmpl = "{{drug_name}} and {{drug_name}} again"
        assert extract_placeholders(tmpl) == ["drug_name"]

    def test_ignores_uppercase(self):
        # only lower-case snake_case keys should be matched
        assert extract_placeholders("{{DRUG_NAME}} {{drug_name}}") == ["drug_name"]

    def test_empty_template(self):
        assert extract_placeholders("No placeholders here.") == []

    def test_sorted_output(self):
        tmpl = "{{z_value}} {{a_value}}"
        assert extract_placeholders(tmpl) == ["a_value", "z_value"]


class TestFillPlaceholders:
    def test_fills_known_key(self):
        result = fill_placeholders("Drug: {{drug_name}}", {"drug_name": "DrugX"})
        assert result == "Drug: DrugX"

    def test_marks_unknown_key(self):
        result = fill_placeholders("Dose: {{dose}}", {})
        assert "[NOT FILLED]" in result

    def test_multiple_placeholders(self):
        tmpl = "{{a}} and {{b}}"
        result = fill_placeholders(tmpl, {"a": "alpha", "b": "beta"})
        assert result == "alpha and beta"

    def test_partial_fill_leaves_marker(self):
        tmpl = "{{a}} and {{b}}"
        result = fill_placeholders(tmpl, {"a": "alpha"})
        assert "alpha" in result
        assert "[NOT FILLED]" in result
