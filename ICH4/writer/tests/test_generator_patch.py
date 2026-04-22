"""Unit tests for patch mode in writer/generator.py.

Tests that write_section() branches correctly between standard fill mode
(no prior_content) and patch mode (prior_content supplied), and that the
correct system/user prompts are sent to the LLM in each case.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GCP_PROJECT_ID", "")

# ── Module-level mocks so GCS and LLM are never hit ───────────────────────────
_mock_gcs = MagicMock()

with patch("google.cloud.storage.Client", return_value=_mock_gcs), \
     patch("langchain_openai.ChatOpenAI"):
    from writer.generator import (  # noqa: E402
        _PATCH_SYSTEM_PROMPT,
        _PATCH_USER_TMPL,
        _SYSTEM_PROMPT,
        write_section,
    )
    from writer.models import ProgramInfo  # noqa: E402


# ── Shared fixtures ───────────────────────────────────────────────────────────

_PROGRAM = ProgramInfo(
    therapeutic_area="neurology",
    disease_type="bells_palsy",
    drug_name="prednisolone",
)


def _make_llm(response_text: str = "## Filled content\n\nSome prose.") -> MagicMock:
    llm = MagicMock()
    resp = MagicMock()
    resp.content = response_text
    resp.tool_calls = []
    llm.invoke.return_value = resp
    return llm


# ── Tests: standard mode (no prior_content) ───────────────────────────────────

class TestWriteSectionStandardMode:
    def test_returns_section_document(self):
        llm = _make_llm("## Overview\n\nNo placeholders here.")
        with patch("writer.generator.build_clinical_context", return_value={}):
            doc = write_section(
                program=_PROGRAM,
                section_key="2.5_clinical_overview",
                section_label="Clinical Overview",
                module_key="module2",
                module_label="Module 2",
                template_content="## Overview\n\nIntro prose.",
                bucket_name="test-bucket",
                llm=llm,
            )
        assert doc.section_key == "2.5_clinical_overview"
        assert doc.module_key  == "module2"
        assert "Overview" in doc.content

    def test_uses_standard_system_prompt(self):
        llm = _make_llm("## Filled")
        captured_messages = []

        def capture_invoke(messages):
            captured_messages.extend(messages)
            r = MagicMock()
            r.content = "## Filled"
            r.tool_calls = []
            return r

        llm.invoke.side_effect = capture_invoke
        with patch("writer.generator.build_clinical_context", return_value={}):
            write_section(
                program=_PROGRAM,
                section_key="2.5_clinical_overview",
                section_label="Clinical Overview",
                module_key="module2",
                module_label="Module 2",
                template_content="## Overview",
                bucket_name="test-bucket",
                llm=llm,
            )

        system_content = captured_messages[0].content
        assert "Fill every {{placeholder}}" in system_content
        # Must NOT include patch-mode instruction
        assert "EXISTING DOCUMENT" not in system_content

    def test_remaining_placeholders_filled_with_pending_marker(self):
        llm = _make_llm("## Overview\n\n{{unfilled_key}} still here.")
        with patch("writer.generator.build_clinical_context", return_value={}):
            doc = write_section(
                program=_PROGRAM,
                section_key="2.5",
                section_label="Overview",
                module_key="module2",
                module_label="Module 2",
                template_content="## Overview\n\n{{unfilled_key}}",
                bucket_name="test-bucket",
                llm=llm,
            )
        assert "[DATA PENDING" in doc.content
        assert "{{unfilled_key}}" not in doc.content


# ── Tests: patch mode (prior_content supplied) ────────────────────────────────

class TestWriteSectionPatchMode:
    def _invoke_patch(
        self,
        prior_text: str = "## Original\n\nOld recovery rate was 60%.",
        changed_keys: list[str] | None = None,
        llm_response: str = "## Updated\n\nNew recovery rate is 72%.",
    ):
        llm = _make_llm()
        captured = []

        def capture(messages):
            captured.extend(messages)
            r = MagicMock()
            r.content = llm_response
            r.tool_calls = []
            return r

        llm.invoke.side_effect = capture

        with patch("writer.generator.build_clinical_context", return_value={}):
            doc = write_section(
                program=_PROGRAM,
                section_key="2.7.3",
                section_label="Summary of Clinical Efficacy",
                module_key="module2",
                module_label="Module 2",
                template_content="## Summary\n\n{{recovery_rate_3mo}}",
                bucket_name="test-bucket",
                llm=llm,
                prior_content=prior_text,
                changed_keys=changed_keys,
            )
        return doc, captured

    def test_uses_patch_system_prompt(self):
        _, messages = self._invoke_patch()
        # System prompt mentions VERBATIM; user prompt contains EXISTING DOCUMENT
        assert "VERBATIM" in messages[0].content
        assert "EXISTING DOCUMENT" in messages[1].content

    def test_does_not_use_standard_system_prompt(self):
        _, messages = self._invoke_patch()
        # Standard prompt instructs "Fill every {{placeholder}}"
        assert "Fill every {{placeholder}}" not in messages[0].content

    def test_prior_content_in_user_message(self):
        prior = "## Original doc\n\nOld rate was 60%."
        _, messages = self._invoke_patch(prior_text=prior)
        assert prior in messages[1].content

    def test_changed_keys_listed_in_user_message(self):
        _, messages = self._invoke_patch(changed_keys=["recovery_rate_3mo", "adverse_events"])
        user_text = messages[1].content
        assert "recovery_rate_3mo" in user_text
        assert "adverse_events"    in user_text

    def test_returns_llm_response_as_content(self):
        doc, _ = self._invoke_patch(llm_response="## Updated\n\nNew recovery rate is 72%.")
        assert "72%" in doc.content

    def test_prior_content_none_uses_standard_mode(self):
        """Passing prior_content=None must fall through to the standard path."""
        llm = _make_llm("## Standard fill")
        captured = []

        def capture(messages):
            captured.extend(messages)
            r = MagicMock()
            r.content = "## Standard fill"
            r.tool_calls = []
            return r

        llm.invoke.side_effect = capture
        with patch("writer.generator.build_clinical_context", return_value={}):
            write_section(
                program=_PROGRAM,
                section_key="2.7.3",
                section_label="Test",
                module_key="module2",
                module_label="Module 2",
                template_content="## Test",
                bucket_name="test-bucket",
                llm=llm,
                prior_content=None,
            )
        # Standard prompt must be used
        assert "Fill every {{placeholder}}" in captured[0].content
