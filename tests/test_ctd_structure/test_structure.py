"""
Tests for ctd_structure/structure.py  (hybrid canonical pipeline)

Covers:
  - JSON parsing helper (_extract_json_object)
  - Canonical tree builder (_build_canonical_modules, _collect_canonical_paths)
  - ICH prefix helpers (_ich_number_prefix, _prefix_is_child)
  - Evaluation helper (_evaluate) — new 5-check single-argument API
  - Email body formatter (_format_email_body)
  - Data model (CTDStructureOutput.to_folder_paths) including sub-subsections
  - All graph node functions (mocking _post_query / SMTP where needed)
  - Routing helper (after_evaluate)
  - Dynamic Module 5 expansion (expand_program_studies)
  - Recovery node (repair_canonical)
  - notify_human (no-email path and SMTP-missing path)
  - refine_from_feedback
  - extract_from_ich_index
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from ctd_structure.structure import (
    CTDModule,
    CTDSection,
    CTDStructureOutput,
    CTDSubsection,
    EnrichState,
    EvaluationIssue,
    EvaluationResult,
    _MAX_REPAIR_RETRIES,
    _build_canonical_modules,
    _collect_canonical_paths,
    _evaluate,
    _extract_json_object,
    _format_email_body,
    _ich_number_prefix,
    _prefix_is_child,
    after_evaluate,
    evaluate_completeness,
    expand_program_studies,
    extract_from_ich_index,
    load_canonical,
    notify_human,
    refine_from_feedback,
    repair_canonical,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _base_state(**overrides) -> EnrichState:
    """Minimal valid EnrichState for unit tests."""
    state: EnrichState = {
        "ich_index_url": "http://localhost:8080",
        "reviewer_email": None,
        "program_context": None,
        "modules": [],
        "evaluation": None,
        "repair_retries": 0,
        "human_notified": False,
    }
    state.update(overrides)
    return state


def _canonical_modules_dicts() -> list[dict]:
    return [m.model_dump() for m in _build_canonical_modules()]


def _passed_evaluation_dict() -> dict:
    return EvaluationResult(passed=True, issues=[], summary="ok").model_dump()


def _failed_evaluation_dict(level: str = "module", path: str = "module1",
                             reason: str = "module missing") -> dict:
    return EvaluationResult(
        passed=False,
        issues=[EvaluationIssue(path=path, level=level, reason=reason)],
        summary="1 issue(s) found",
    ).model_dump()


# ── _extract_json_object ──────────────────────────────────────────────────────

class TestExtractJsonObject:
    def test_plain_json(self):
        text = '{"key": "module1", "label": "Administrative"}'
        result = _extract_json_object(text)
        assert result == {"key": "module1", "label": "Administrative"}

    def test_wrapped_in_markdown_fences(self):
        text = '```json\n{"a": 1}\n```'
        assert _extract_json_object(text) == {"a": 1}

    def test_trailing_prose_ignored(self):
        text = 'Here is the object: {"x": "y"} and some prose.'
        assert _extract_json_object(text) == {"x": "y"}

    def test_returns_none_for_no_braces(self):
        assert _extract_json_object("no json here") is None

    def test_returns_none_for_invalid_json(self):
        assert _extract_json_object("{bad json}") is None

    def test_empty_object(self):
        assert _extract_json_object("{}") == {}


# ── ICH prefix helpers ────────────────────────────────────────────────────────

class TestIchPrefixHelpers:
    def test_number_prefix_standard(self):
        assert _ich_number_prefix("3.2.S.1_general_information") == "3.2.S.1"

    def test_number_prefix_deep(self):
        assert _ich_number_prefix("4.2.3.3.1_in_vitro") == "4.2.3.3.1"

    def test_number_prefix_no_dot(self):
        assert _ich_number_prefix("module3") == ""

    def test_number_prefix_program_specific(self):
        assert _ich_number_prefix("study_001_phase3_t2d") == ""

    def test_number_prefix_no_underscore(self):
        assert _ich_number_prefix("nodeunderscore") == ""

    def test_prefix_is_child_valid(self):
        assert _prefix_is_child("3.2.S", "3.2.S.1_general_information") is True

    def test_prefix_is_child_deeply_nested(self):
        assert _prefix_is_child("4.2.3", "4.2.3.3_genotoxicity") is True

    def test_prefix_is_child_mismatch(self):
        # full keys with underscore required so _ich_number_prefix extracts a prefix
        assert _prefix_is_child("4.2.3_toxicology", "3.2.S.1_general_information") is False

    def test_prefix_is_child_no_parent_prefix(self):
        assert _prefix_is_child("module3", "3.2.S_drug_substance") is True

    def test_prefix_is_child_no_child_prefix(self):
        assert _prefix_is_child("3.2.S", "study_001_ba") is True


# ── CTDStructureOutput.to_folder_paths ─────────────────────────────────────────

class TestToFolderPaths:
    def test_full_hierarchy(self):
        output = CTDStructureOutput(modules=[
            CTDModule(
                key="module1", label="Admin",
                sections=[
                    CTDSection(
                        key="1.1_toc", label="TOC",
                        subsections=[CTDSubsection(key="1.1.1_index", label="Index")],
                    ),
                ],
            ),
        ])
        paths = output.to_folder_paths()
        assert "ctd/module1/" in paths
        assert "ctd/module1/1.1_toc/" in paths
        assert "ctd/module1/1.1_toc/1.1.1_index/" in paths

    def test_module_with_no_sections(self):
        output = CTDStructureOutput(modules=[
            CTDModule(key="module5", label="Clinical", sections=[]),
        ])
        paths = output.to_folder_paths()
        assert paths == ["ctd/module5/"]

    def test_section_with_no_subsections(self):
        output = CTDStructureOutput(modules=[
            CTDModule(key="module2", label="CTD Summaries", sections=[
                CTDSection(key="2.1_toc", label="TOC", subsections=[]),
            ]),
        ])
        paths = output.to_folder_paths()
        assert "ctd/module2/" in paths
        assert "ctd/module2/2.1_toc/" in paths
        assert len(paths) == 2

    def test_sub_subsections_flattened(self):
        """4th-level sub-subsections appear as paths."""
        output = CTDStructureOutput(modules=[
            CTDModule(key="module3", label="Quality", sections=[
                CTDSection(key="3.2.S_drug_substance", label="DS", subsections=[
                    CTDSubsection(
                        key="3.2.S.1_general_information", label="General",
                        sub_subsections=[
                            CTDSubsection(key="3.2.S.1.1_nomenclature", label="Nomenclature"),
                        ],
                    ),
                ]),
            ]),
        ])
        paths = output.to_folder_paths()
        assert "ctd/module3/3.2.S_drug_substance/3.2.S.1_general_information/" in paths
        assert (
            "ctd/module3/3.2.S_drug_substance/3.2.S.1_general_information/"
            "3.2.S.1.1_nomenclature/"
        ) in paths

    def test_depth_5_program_studies(self):
        """Program-specific leaf folders injected at depth 5 are also included."""
        output = CTDStructureOutput(modules=[
            CTDModule(key="module5", label="Clinical", sections=[
                CTDSection(key="5.3_clinical_study_reports", label="CSR", subsections=[
                    CTDSubsection(
                        key="5.3.1_reports_of_biopharmaceutic_studies", label="Bio",
                        sub_subsections=[
                            CTDSubsection(
                                key="5.3.1.1_bioavailability_study_reports", label="BA",
                                sub_subsections=[
                                    CTDSubsection(key="study_001_ba", label="Study 001 BA"),
                                ],
                            ),
                        ],
                    ),
                ]),
            ]),
        ])
        paths = output.to_folder_paths()
        assert any("study_001_ba/" in p for p in paths)


# ── _build_canonical_modules ──────────────────────────────────────────────────

class TestBuildCanonicalModules:
    def test_returns_five_modules(self):
        modules = _build_canonical_modules()
        assert len(modules) == 5

    def test_module_keys_correct(self):
        modules = _build_canonical_modules()
        keys = [m.key for m in modules]
        assert keys == ["module1", "module2", "module3", "module4", "module5"]

    def test_total_path_count(self):
        """Canonical template must produce at least 198 GCS paths (ICH M4 R4)."""
        modules = _build_canonical_modules()
        output = CTDStructureOutput(modules=modules)
        paths = output.to_folder_paths()
        assert len(paths) >= 198, f"Expected ≥198 paths, got {len(paths)}"

    def test_module3_has_drug_substance_and_drug_product(self):
        modules = _build_canonical_modules()
        m3 = next(m for m in modules if m.key == "module3")
        sec_keys = [s.key for s in m3.sections]
        assert "3.2.S_drug_substance" in sec_keys
        assert "3.2.P_drug_product" in sec_keys

    def test_module5_has_clinical_study_reports_section(self):
        modules = _build_canonical_modules()
        m5 = next(m for m in modules if m.key == "module5")
        sec_keys = [s.key for s in m5.sections]
        assert "5.3_clinical_study_reports" in sec_keys

    def test_sub_subsections_present_in_module3(self):
        modules = _build_canonical_modules()
        m3 = next(m for m in modules if m.key == "module3")
        ds = next(s for s in m3.sections if s.key == "3.2.S_drug_substance")
        gen_info = next(sub for sub in ds.subsections if sub.key == "3.2.S.1_general_information")
        assert len(gen_info.sub_subsections) == 3
        subkeys = [ss.key for ss in gen_info.sub_subsections]
        assert "3.2.S.1.1_nomenclature" in subkeys
        assert "3.2.S.1.2_structure" in subkeys
        assert "3.2.S.1.3_general_properties" in subkeys

    def test_collect_canonical_paths_matches_to_folder_paths(self):
        """_collect_canonical_paths should cover all paths produced by to_folder_paths."""
        modules = _build_canonical_modules()
        output = CTDStructureOutput(modules=modules)
        folder_paths = {p.removeprefix("ctd/").rstrip("/") for p in output.to_folder_paths()}
        canonical = _collect_canonical_paths()
        missing = folder_paths - canonical
        assert not missing, f"Paths not in _collect_canonical_paths(): {missing}"


# ── _evaluate ─────────────────────────────────────────────────────────────────

class TestEvaluate:
    def test_canonical_build_passes(self):
        """The unmodified canonical build must pass all 5 checks."""
        modules = _build_canonical_modules()
        result = _evaluate(modules)
        assert result.passed is True, result.summary
        assert result.issues == []

    def test_summary_on_pass(self):
        modules = _build_canonical_modules()
        result = _evaluate(modules)
        assert "modules" in result.summary
        assert "sections" in result.summary
        assert "subsections" in result.summary

    def test_missing_module_flagged(self):
        modules = [m for m in _build_canonical_modules() if m.key != "module3"]
        result = _evaluate(modules)
        assert result.passed is False
        missing = [i for i in result.issues if i.path == "module3" and i.level == "module"]
        assert missing, "module3 missing issue not found"

    def test_empty_module_flagged(self):
        modules = _build_canonical_modules()
        idx = next(i for i, m in enumerate(modules) if m.key == "module2")
        modules[idx] = CTDModule(key="module2", label="CTD Summaries", sections=[])
        result = _evaluate(modules)
        assert result.passed is False
        empty = [i for i in result.issues if i.path == "module2" and "zero sections" in i.reason]
        assert empty

    def test_duplicate_section_flagged(self):
        modules = _build_canonical_modules()
        m2 = next(m for m in modules if m.key == "module2")
        dup_sec = m2.sections[0]
        new_m2 = m2.model_copy(update={"sections": list(m2.sections) + [dup_sec]})
        modules = [new_m2 if m.key == "module2" else m for m in modules]
        result = _evaluate(modules)
        assert result.passed is False
        dup = [i for i in result.issues if "duplicate" in i.reason and i.level == "section"]
        assert dup

    def test_duplicate_subsection_flagged(self):
        modules = _build_canonical_modules()
        m2 = next(m for m in modules if m.key == "module2")
        sec = next(s for s in m2.sections if s.key == "2.5_clinical_overview")
        dup_sub = sec.subsections[0]
        new_sec = sec.model_copy(update={"subsections": list(sec.subsections) + [dup_sub]})
        new_m2 = m2.model_copy(update={
            "sections": [new_sec if s.key == sec.key else s for s in m2.sections]
        })
        modules = [new_m2 if m.key == "module2" else m for m in modules]
        result = _evaluate(modules)
        assert result.passed is False
        dup = [i for i in result.issues if "duplicate" in i.reason and i.level == "subsection"]
        assert dup

    def test_ich_prefix_mismatch_flagged(self):
        wrong_sub = CTDSubsection(key="4.2.1_wrong_parent", label="Wrong")
        section = CTDSection(key="2.5_clinical_overview", label="CO", subsections=[wrong_sub])
        mod = CTDModule(key="module2", label="CTD", sections=[section])
        modules = [m for m in _build_canonical_modules() if m.key != "module2"] + [mod]
        result = _evaluate(modules)
        assert result.passed is False
        prefix_issues = [i for i in result.issues if "prefix mismatch" in i.reason.lower()]
        assert prefix_issues

    def test_canonical_completeness_check_flags_removal(self):
        """Removing a canonical path causes a completeness failure."""
        modules = _build_canonical_modules()
        m3 = next(m for m in modules if m.key == "module3")
        new_m3 = m3.model_copy(update={"sections": m3.sections[1:]})
        modules = [new_m3 if m.key == "module3" else m for m in modules]
        result = _evaluate(modules)
        assert result.passed is False
        completeness = [i for i in result.issues if "canonical ICH" in i.reason]
        assert completeness


# ── _format_email_body ────────────────────────────────────────────────────────

class TestFormatEmailBody:
    def test_passed_status_in_body(self):
        modules = [CTDModule(key="module1", label="Admin", sections=[])]
        evaluation = EvaluationResult(passed=True, issues=[], summary="All good.")
        body = _format_email_body(modules, evaluation)
        assert "PASSED" in body
        assert "module1/" in body

    def test_issues_annotated_inline(self):
        modules = [CTDModule(key="module1", label="Admin", sections=[])]
        evaluation = EvaluationResult(
            passed=False,
            issues=[EvaluationIssue(path="module1", level="module", reason="empty")],
            summary="1 issue",
        )
        body = _format_email_body(modules, evaluation)
        assert "ISSUES FOUND" in body
        assert "◄ empty" in body

    def test_approve_reject_footer_present(self):
        evaluation = EvaluationResult(passed=True, issues=[], summary="")
        body = _format_email_body([], evaluation)
        assert "APPROVE or REJECT" in body

    def test_sub_subsections_rendered(self):
        modules = [CTDModule(key="module3", label="Quality", sections=[
            CTDSection(key="3.2.S_drug_substance", label="DS", subsections=[
                CTDSubsection(
                    key="3.2.S.1_general_information", label="General",
                    sub_subsections=[
                        CTDSubsection(key="3.2.S.1.1_nomenclature", label="Nomenclature"),
                    ],
                ),
            ]),
        ])]
        evaluation = EvaluationResult(passed=True, issues=[], summary="ok")
        body = _format_email_body(modules, evaluation)
        assert "3.2.S.1.1_nomenclature/" in body


# ── Graph node: load_canonical ────────────────────────────────────────────────

class TestLoadCanonical:
    def test_produces_five_modules(self):
        result = load_canonical(_base_state())
        assert len(result["modules"]) == 5

    def test_module_keys_present(self):
        result = load_canonical(_base_state())
        keys = [m["key"] for m in result["modules"]]
        assert set(keys) == {"module1", "module2", "module3", "module4", "module5"}

    def test_output_is_list_of_dicts(self):
        result = load_canonical(_base_state())
        assert all(isinstance(m, dict) for m in result["modules"])

    def test_module3_has_sub_subsections(self):
        result = load_canonical(_base_state())
        m3 = next(m for m in result["modules"] if m["key"] == "module3")
        ds = next(s for s in m3["sections"] if s["key"] == "3.2.S_drug_substance")
        gen = next(sub for sub in ds["subsections"] if sub["key"] == "3.2.S.1_general_information")
        assert len(gen["sub_subsections"]) > 0


# ── Graph node: expand_program_studies ───────────────────────────────────────

class TestExpandProgramStudies:
    def _state_with_modules(self, program_context=None) -> EnrichState:
        return _base_state(modules=_canonical_modules_dicts(), program_context=program_context)

    def test_noop_when_no_context(self):
        result = expand_program_studies(self._state_with_modules(program_context=None))
        assert result == {}

    def test_noop_when_studies_empty(self):
        result = expand_program_studies(self._state_with_modules(program_context={"studies": {}}))
        assert result == {}

    def test_injects_study_under_correct_leaf(self):
        ctx = {
            "studies": {
                "5.3.1.1_bioavailability_study_reports": [
                    {"key": "study_001_ba_fasting", "label": "Study 001 BA Fasting"},
                ],
            }
        }
        result = expand_program_studies(self._state_with_modules(program_context=ctx))
        assert "modules" in result
        m5 = next(m for m in result["modules"] if m["key"] == "module5")
        csr = next(s for s in m5["sections"] if s["key"] == "5.3_clinical_study_reports")
        bio = next(sub for sub in csr["subsections"]
                   if sub["key"] == "5.3.1_reports_of_biopharmaceutic_studies")
        ba = next(ss for ss in bio["sub_subsections"]
                  if ss["key"] == "5.3.1.1_bioavailability_study_reports")
        assert "study_001_ba_fasting" in [s["key"] for s in ba["sub_subsections"]]

    def test_only_module5_is_mutated(self):
        ctx = {
            "studies": {
                "5.3.1.1_bioavailability_study_reports": [
                    {"key": "study_001", "label": "Study 001"},
                ],
            }
        }
        original_m3 = next(m for m in _canonical_modules_dicts() if m["key"] == "module3")
        result = expand_program_studies(self._state_with_modules(program_context=ctx))
        new_m3 = next(m for m in result["modules"] if m["key"] == "module3")
        assert new_m3 == original_m3

    def test_entry_without_key_skipped(self):
        ctx = {
            "studies": {
                "5.3.1.1_bioavailability_study_reports": [
                    {"label": "missing key entry"},
                    {"key": "study_002", "label": "Study 002"},
                ],
            }
        }
        result = expand_program_studies(self._state_with_modules(program_context=ctx))
        m5 = next(m for m in result["modules"] if m["key"] == "module5")
        csr = next(s for s in m5["sections"] if s["key"] == "5.3_clinical_study_reports")
        bio = next(sub for sub in csr["subsections"]
                   if sub["key"] == "5.3.1_reports_of_biopharmaceutic_studies")
        ba = next(ss for ss in bio["sub_subsections"]
                  if ss["key"] == "5.3.1.1_bioavailability_study_reports")
        study_keys = [s["key"] for s in ba["sub_subsections"]]
        assert "study_002" in study_keys
        assert not any(k == "" or k is None for k in study_keys)


# ── Graph node: evaluate_completeness ────────────────────────────────────────

class TestEvaluateCompleteness:
    def test_canonical_modules_pass(self):
        state = _base_state(modules=_canonical_modules_dicts())
        result = evaluate_completeness(state)
        ev = EvaluationResult(**result["evaluation"])
        assert ev.passed is True

    def test_empty_modules_fail(self):
        state = _base_state(modules=[])
        result = evaluate_completeness(state)
        ev = EvaluationResult(**result["evaluation"])
        assert ev.passed is False

    def test_returns_evaluation_dict(self):
        state = _base_state(modules=_canonical_modules_dicts())
        result = evaluate_completeness(state)
        assert "evaluation" in result
        assert isinstance(result["evaluation"], dict)
        assert "passed" in result["evaluation"]


# ── Graph node: notify_human ──────────────────────────────────────────────────

class TestNotifyHuman:
    def test_no_reviewer_returns_not_notified(self, monkeypatch):
        monkeypatch.delenv("CTD_REVIEWER_EMAIL", raising=False)
        state = _base_state(
            modules=_canonical_modules_dicts(),
            evaluation=_passed_evaluation_dict(),
            reviewer_email=None,
        )
        result = notify_human(state)
        assert result["human_notified"] is False

    def test_smtp_missing_env_returns_not_notified(self, monkeypatch):
        monkeypatch.setenv("CTD_REVIEWER_EMAIL", "reviewer@example.com")
        monkeypatch.delenv("SMTP_USER", raising=False)
        state = _base_state(
            modules=_canonical_modules_dicts(),
            evaluation=_passed_evaluation_dict(),
            reviewer_email="reviewer@example.com",
        )
        result = notify_human(state)
        assert result["human_notified"] is False

    def test_successful_send_returns_notified(self, monkeypatch):
        monkeypatch.setenv("CTD_REVIEWER_EMAIL", "reviewer@example.com")
        monkeypatch.setenv("SMTP_USER", "sender@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
        state = _base_state(
            modules=_canonical_modules_dicts(),
            evaluation=_passed_evaluation_dict(),
            reviewer_email="reviewer@example.com",
        )
        with patch("ctd_structure.structure._send_approval_email") as mock_send:
            result = notify_human(state)
        mock_send.assert_called_once()
        assert result["human_notified"] is True

    def test_no_evaluation_still_sends(self, monkeypatch):
        monkeypatch.setenv("CTD_REVIEWER_EMAIL", "r@example.com")
        monkeypatch.setenv("SMTP_USER", "s@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        state = _base_state(
            modules=_canonical_modules_dicts(),
            evaluation=None,
            reviewer_email="r@example.com",
        )
        with patch("ctd_structure.structure._send_approval_email"):
            result = notify_human(state)
        assert result["human_notified"] is True


# ── Graph node: repair_canonical ─────────────────────────────────────────────

class TestRepairCanonical:
    def test_rebuilds_flagged_module(self):
        mods = _canonical_modules_dicts()
        for m in mods:
            if m["key"] == "module2":
                m["sections"] = []
        ev = _failed_evaluation_dict(level="module", path="module2",
                                     reason="module has zero sections")
        result = repair_canonical(_base_state(modules=mods, evaluation=ev, repair_retries=0))
        repaired_m2 = next(m for m in result["modules"] if m["key"] == "module2")
        assert len(repaired_m2["sections"]) > 0

    def test_repair_increments_retries(self):
        ev = _failed_evaluation_dict()
        result = repair_canonical(
            _base_state(modules=_canonical_modules_dicts(), evaluation=ev, repair_retries=0)
        )
        assert result["repair_retries"] == 1

    def test_missing_module_is_re_added(self):
        mods = [m for m in _canonical_modules_dicts() if m["key"] != "module1"]
        ev = _failed_evaluation_dict(path="module1", reason="module missing")
        result = repair_canonical(_base_state(modules=mods, evaluation=ev, repair_retries=0))
        assert "module1" in {m["key"] for m in result["modules"]}

    def test_unflagged_modules_preserved(self):
        mods = _canonical_modules_dicts()
        for m in mods:
            if m["key"] == "module3":
                m["label"] = "CUSTOM LABEL"
        ev = _failed_evaluation_dict(path="module2", reason="module has zero sections")
        result = repair_canonical(_base_state(modules=mods, evaluation=ev, repair_retries=0))
        m3 = next(m for m in result["modules"] if m["key"] == "module3")
        assert m3["label"] == "CUSTOM LABEL"


# ── Routing: after_evaluate ───────────────────────────────────────────────────

class TestAfterEvaluate:
    def test_routes_to_repair_on_structural_issue(self):
        ev = _failed_evaluation_dict(level="module", path="module1",
                                     reason="module missing from canonical template")
        assert after_evaluate(_base_state(evaluation=ev, repair_retries=0)) == "repair_canonical"

    def test_routes_to_notify_when_budget_exhausted(self):
        ev = _failed_evaluation_dict(level="module", path="module1",
                                     reason="module missing from canonical template")
        assert after_evaluate(
            _base_state(evaluation=ev, repair_retries=_MAX_REPAIR_RETRIES)
        ) == "notify_human"

    def test_routes_to_notify_on_passed_evaluation(self):
        ev = _passed_evaluation_dict()
        assert after_evaluate(_base_state(evaluation=ev, repair_retries=0)) == "notify_human"

    def test_routes_to_notify_on_advisory_issues_only(self):
        ev = EvaluationResult(
            passed=False,
            issues=[
                EvaluationIssue(path="module2/2.5_co/3.1_wrong", level="subsection",
                                reason="ICH prefix mismatch: not a child"),
            ],
            summary="1 issue",
        ).model_dump()
        assert after_evaluate(_base_state(evaluation=ev, repair_retries=0)) == "notify_human"

    def test_canonical_completeness_issue_triggers_repair(self):
        ev = EvaluationResult(
            passed=False,
            issues=[
                EvaluationIssue(
                    path="module3/3.1_table_of_contents",
                    level="section",
                    reason="canonical ICH M4(R4) path missing from assembled structure",
                ),
            ],
            summary="1 issue",
        ).model_dump()
        assert after_evaluate(
            _base_state(evaluation=ev, repair_retries=0)
        ) == "repair_canonical"

    def test_max_repair_retries_constant(self):
        assert isinstance(_MAX_REPAIR_RETRIES, int)
        assert _MAX_REPAIR_RETRIES >= 1


# ── refine_from_feedback ──────────────────────────────────────────────────────

class TestRefineFromFeedback:
    def test_returns_ctd_structure_output(self):
        with patch("ctd_structure.structure._post_query", return_value=""), \
             patch("ctd_structure.structure._gcloud_token", return_value=""):
            output, evaluation = refine_from_feedback(
                "http://localhost", "some feedback",
                CTDStructureOutput(modules=_build_canonical_modules()),
            )
        assert isinstance(output, CTDStructureOutput)
        assert isinstance(evaluation, EvaluationResult)

    def test_canonical_structure_preserved_after_refine(self):
        with patch("ctd_structure.structure._post_query", return_value=""), \
             patch("ctd_structure.structure._gcloud_token", return_value=""):
            output, evaluation = refine_from_feedback(
                "http://localhost", "feedback text",
                CTDStructureOutput(modules=_build_canonical_modules()),
            )
        assert len(output.modules) == 5
        assert evaluation.passed is True

    def test_network_error_during_refine_does_not_raise(self):
        with patch("ctd_structure.structure._post_query",
                   side_effect=Exception("timeout")), \
             patch("ctd_structure.structure._gcloud_token", return_value=""):
            output, evaluation = refine_from_feedback(
                "http://localhost", "feedback",
                CTDStructureOutput(modules=_build_canonical_modules()),
            )
        assert len(output.modules) == 5


# ── extract_from_ich_index ────────────────────────────────────────────────────

class TestExtractFromIchIndex:
    def test_returns_five_modules_no_url(self):
        output, evaluation = extract_from_ich_index("")
        assert len(output.modules) == 5
        assert evaluation.passed is True

    def test_evaluation_passed_for_canonical_build(self):
        output, evaluation = extract_from_ich_index("")
        assert evaluation.passed is True, evaluation.summary

    def test_program_context_injects_study(self):
        ctx = {
            "studies": {
                "5.3.1.1_bioavailability_study_reports": [
                    {"key": "study_001_ba", "label": "Study 001 BA"},
                ],
            }
        }
        output, _ = extract_from_ich_index("", program_context=ctx)
        paths = output.to_folder_paths()
        assert any("study_001_ba/" in p for p in paths)

