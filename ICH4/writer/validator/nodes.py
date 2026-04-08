"""Validator nodes — each performs one focused cross-module consistency check.

Node pipeline:
  1. extract_key_values            — regex-extract structured facts from every section
  2. check_consistency             — compare each section's values against cross-section
                                     consensus (drug name, demographics, efficacy, safety)
  3. auto_fill_from_cross_sections — replace [DATA PENDING] markers where canonical values
                                     are available from other sections
  4. check_unfilled_placeholders   — flag any remaining [DATA PENDING] (error)
  5. check_not_filled_markers      — flag any remaining {{key}} [NOT FILLED] (error)
  6. llm_deep_check                — LLM narrative coherence + remaining contradiction scan
  7. finalise                      — produce passed/failed verdict + summary
"""
from __future__ import annotations

import logging
import re
from collections import Counter

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from writer.models import ValidationIssue
from .state import ValidatorState

logger = logging.getLogger(__name__)

# ── Regex patterns for structured fact extraction ─────────────────────────────

# Drug / compound name — grab a proper-noun-style token after a trigger word
_RE_DRUG_NAME = re.compile(
    r"(?:compound|drug|product|treatment|agent|inhibitor|molecule)"
    r"[:\s'\"]+([A-Z][A-Za-z0-9\-]{2,})",
)

# Study size N
_RE_STUDY_N = [
    re.compile(r"\bN\s*=\s*(\d{1,5})\b"),
    re.compile(r"\b(\d{1,5})\s+(?:patients|subjects|participants|enrolled)\b", re.I),
    re.compile(r"(?:enrolled|comprising|included)\s+(\d{1,5})\s+(?:patients|subjects)", re.I),
    re.compile(r"(?:total\s+of|randomised|randomized)\s+(\d{1,5})\s+(?:patients|subjects)", re.I),
]

# Mean / median age
_RE_AGE = [
    re.compile(r"(?:mean|median)\s+age\s+(?:was|of|:)\s+(\d+\.?\d*)\s*years", re.I),
    re.compile(r"age\s+(?:of\s+)?(\d+\.?\d*)\s*(?:±|\+/-).*?years", re.I),
]

# Male proportion
_RE_MALE_PCT = [
    re.compile(r"(\d+\.?\d*)\s*%\s+(?:were\s+)?male\b", re.I),
    re.compile(r"\bmale[:\s]+(\d+\.?\d*)\s*%", re.I),
]

# Efficacy endpoints
_RE_EFFICACY = {
    key: re.compile(rf"\b{key}\b.*?(\d+\.?\d*)\s*%", re.I)
    for key in ("ORR", "DCR", "CR", "PR", "OS", "PFS", "EFS", "RFS", "DOR")
}

# p-value
_RE_PVALUE = re.compile(r"\bp\s*[<=>]\s*(0\.\d+)\b", re.I)

# AE rate
_RE_AE_RATE = re.compile(
    r"(\d+\.?\d*)\s*%.*?(?:adverse\s+event|AE|treatment-emergent)", re.I
)

# Patterns for auto_fill_from_cross_sections
# Each tuple: (compiled_pattern, canonical_field)
_PENDING = r"\[DATA PENDING[^\]]*\]"
_FILL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Drug name
    (re.compile(
        r"(?:compound|drug|product|treatment|agent)(\s+)" + _PENDING,
        re.I), "drug_name"),
    (re.compile(
        _PENDING + r"(\s+was\s+(?:administered|given|studied))",
        re.I), "drug_name"),
    # Study N
    (re.compile(r"(\bN\s*=\s*)" + _PENDING, re.I), "study_n"),
    (re.compile(
        r"(?:enrolled|total\s+of|comprising)\s+" + _PENDING + r"(\s+(?:patients|subjects))",
        re.I), "study_n"),
    # Mean age
    (re.compile(
        r"(?:mean|median)\s+age\s+(?:was|of)\s+" + _PENDING + r"(\s+years)",
        re.I), "mean_age"),
    # Male %
    (re.compile(_PENDING + r"(\s*%\s+(?:were\s+)?male)", re.I), "male_pct"),
    # p-value
    (re.compile(r"\bp\s*[<=>]\s*" + _PENDING, re.I), "p_value"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _first_match(patterns: list, text: str) -> str | None:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def _consensus(values: list[str]) -> str | None:
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


# ── Node 1: extract_key_values ────────────────────────────────────────────────

def extract_key_values(state: ValidatorState) -> dict:
    """Parse structured facts from every document section, then compute consensus."""
    extracted: dict[str, dict[str, str]] = {}

    for doc in state.documents:
        data: dict[str, str] = {}
        text = doc.content

        m = _RE_DRUG_NAME.search(text)
        if m:
            data["drug_name"] = m.group(1).strip()

        v = _first_match(_RE_STUDY_N, text)
        if v:
            data["study_n"] = v

        v = _first_match(_RE_AGE, text)
        if v:
            data["mean_age"] = v

        v = _first_match(_RE_MALE_PCT, text)
        if v:
            data["male_pct"] = v

        for endpoint, pat in _RE_EFFICACY.items():
            m2 = pat.search(text)
            if m2:
                data[f"efficacy_{endpoint.lower()}"] = m2.group(1).strip()
                break

        m3 = _RE_PVALUE.search(text)
        if m3:
            data["p_value"] = m3.group(1).strip()

        m4 = _RE_AE_RATE.search(text)
        if m4:
            data["ae_rate"] = m4.group(1).strip()

        if data:
            extracted[doc.section_key] = data

    # Compute canonical (consensus) value per field
    all_fields: set[str] = set()
    for sec_data in extracted.values():
        all_fields.update(sec_data.keys())

    canonical: dict[str, str] = {}
    for field in all_fields:
        vals = [sec_data[field] for sec_data in extracted.values() if field in sec_data]
        v = _consensus(vals)
        if v:
            canonical[field] = v

    logger.info("[validator] Canonical values extracted: %s", canonical)
    return {
        "extracted_values": extracted,
        "canonical_values": canonical,
        "drug_name_found": canonical.get("drug_name", ""),
    }


# ── Node 2: check_consistency ─────────────────────────────────────────────────

_SEVERITY_MAP: dict[str, str] = {
    "drug_name":  "error",
    "study_n":    "warning",
    "mean_age":   "warning",
    "male_pct":   "warning",
    "p_value":    "warning",
    "ae_rate":    "warning",
}
_LABEL_MAP: dict[str, str] = {
    "drug_name":  "Drug / compound name",
    "study_n":    "Study population (N)",
    "mean_age":   "Mean age (years)",
    "male_pct":   "Male proportion (%)",
    "p_value":    "p-value",
    "ae_rate":    "AE rate (%)",
}


def check_consistency(state: ValidatorState) -> dict:
    """Compare per-section extracted values against the cross-section consensus.

    Flags:
    - Drug name mismatch                → error
    - Efficacy value mismatch           → error
    - Demographic / safety mismatch     → warning
    - Minor rounding difference (≤5%)   → info
    """
    issues: list[ValidationIssue] = []
    extracted = state.extracted_values
    canonical = state.canonical_values

    if not extracted or not canonical:
        return {"issues": issues}

    for section_key, sec_data in extracted.items():
        for field, value in sec_data.items():
            canon = canonical.get(field)
            if not canon or value == canon:
                continue

            # Numeric proximity — small rounding differences down-graded to info
            try:
                diff = abs(float(value) - float(canon))
                relative = diff / max(abs(float(canon)), 1e-9)
                if relative < 0.02:      # ≤2% — likely rounding, skip
                    continue
                if relative < 0.05:      # 2–5% — info only
                    label = _LABEL_MAP.get(field, field.replace("_", " ").title())
                    issues.append(ValidationIssue(
                        severity="info",
                        section_key=section_key,
                        message=(
                            f"{label}: minor rounding difference — "
                            f"this section uses '{value}', consensus is '{canon}'."
                        ),
                    ))
                    continue
            except ValueError:
                pass  # non-numeric (e.g. drug name) — fall through

            label = _LABEL_MAP.get(field, field.replace("_", " ").title())
            sev = (
                "error"
                if (field == "drug_name" or field.startswith("efficacy_"))
                else _SEVERITY_MAP.get(field, "warning")
            )
            issues.append(ValidationIssue(
                severity=sev,   # type: ignore[arg-type]
                section_key=section_key,
                message=(
                    f"{label} inconsistency: this section states '{value}', "
                    f"but cross-section consensus is '{canon}'. "
                    "Verify and align all sections."
                ),
            ))

    return {"issues": issues}


# ── Node 3: auto_fill_from_cross_sections ─────────────────────────────────────

def auto_fill_from_cross_sections(state: ValidatorState) -> dict:
    """Replace [DATA PENDING] markers using canonical values where context is unambiguous.

    Returns updated documents and INFO issues for every auto-fill performed so
    the author can review what was substituted.
    """
    canonical = state.canonical_values
    if not canonical:
        return {"documents": state.documents, "issues": []}

    updated_docs = []
    fill_issues: list[ValidationIssue] = []

    for doc in state.documents:
        if "[DATA PENDING" not in doc.content:
            updated_docs.append(doc)
            continue

        content = doc.content
        fills: list[str] = []

        for (pat, field) in _FILL_PATTERNS:
            value = canonical.get(field)
            if not value:
                continue

            def _replace(m: re.Match, _val: str = value) -> str:
                matched = m.group(0)
                pending = re.search(_PENDING, matched)
                return matched.replace(pending.group(0), _val) if pending else matched

            new_content = pat.sub(_replace, content)
            if new_content != content:
                fills.append(f"{field}='{value}'")
                content = new_content

        if fills:
            fill_issues.append(ValidationIssue(
                severity="info",
                section_key=doc.section_key,
                message=(
                    "Auto-filled from cross-section consensus: "
                    + ", ".join(fills)
                    + ". Please review auto-filled values."
                ),
            ))

        updated_docs.append(doc.model_copy(update={"content": content}))

    logger.info(
        "[validator] auto_fill applied to %d section(s).",
        len(fill_issues),
    )
    return {"documents": updated_docs, "issues": fill_issues}


# ── Node 4: check_unfilled_placeholders ───────────────────────────────────────

def check_unfilled_placeholders(state: ValidatorState) -> dict:
    """Flag any section that still contains [DATA PENDING] markers after auto-fill."""
    issues: list[ValidationIssue] = []
    for doc in state.documents:
        count = doc.content.count("[DATA PENDING")
        if count:
            issues.append(ValidationIssue(
                severity="error",
                section_key=doc.section_key,
                message=(
                    f"{count} [DATA PENDING] placeholder(s) remain — "
                    "author must supply data before submission."
                ),
            ))
    return {"issues": issues}


# ── Node 5: check_not_filled_markers ──────────────────────────────────────────

def check_not_filled_markers(state: ValidatorState) -> dict:
    """Flag sections where the LLM left {{key}} [NOT FILLED] markers."""
    issues: list[ValidationIssue] = []
    pattern = re.compile(r"\{\{[a-z_]+\}\}\s*\[NOT FILLED\]")
    for doc in state.documents:
        hits = pattern.findall(doc.content)
        if hits:
            issues.append(ValidationIssue(
                severity="error",
                section_key=doc.section_key,
                message=f"NOT FILLED markers present: {', '.join(hits)}",
            ))
    return {"issues": issues}


# ── Node 6: LLM deep check ────────────────────────────────────────────────────

_CONSISTENCY_SYSTEM = """\
You are a senior regulatory reviewer doing a cross-module consistency audit.
You will receive excerpts from CTD sections plus any known consensus values.

Check for:
1. DRUG_NAME   — drug/compound name stated differently across sections.
2. DEMOGRAPHICS — patient N, age, gender ratio inconsistent across sections.
3. EFFICACY    — conflicting efficacy claims (e.g. ORR 68% in 2.5 vs 72% in 2.7).
4. SAFETY      — conflicting safety data (AE or SAE rates differ between sections).
5. CROSS_REF   — a section references data still marked [DATA PENDING] that is clearly
                 stated in another section.
6. NARRATIVE   — contradictory language (e.g. "well tolerated" vs "significant toxicity").

For EACH issue found respond with exactly one line:
  ISSUE|<severity>|<section_key>|<message>
<severity>: error, warning, or info
If no issues are found respond with exactly: NO_ISSUES
No other text.
"""


def llm_deep_check(state: ValidatorState, llm: ChatOpenAI) -> dict:
    """Ask the LLM to catch semantic inconsistencies the rule-based nodes may have missed."""
    if len(state.documents) < 2:
        return {"issues": [], "summary": "Single section — no cross-module LLM check performed."}

    digest_parts: list[str] = []
    for doc in state.documents:
        snippet = doc.content[:500].replace("\n", " ")
        digest_parts.append(f"[{doc.section_key}] {doc.section_label}:\n{snippet}")
    digest = "\n\n".join(digest_parts)

    if state.canonical_values:
        cv_text = ", ".join(f"{k}={v}" for k, v in state.canonical_values.items())
        digest += f"\n\nCross-section consensus values: {cv_text}"

    messages = [
        SystemMessage(content=_CONSISTENCY_SYSTEM),
        HumanMessage(content=f"Section excerpts:\n\n{digest}"),
    ]

    try:
        response = llm.invoke(messages)
        raw = response.content.strip()
    except Exception as exc:
        logger.warning("[validator] LLM deep-check failed: %s", exc)
        return {"issues": [], "summary": f"LLM check skipped: {exc}"}

    if raw == "NO_ISSUES":
        return {"issues": [], "summary": "LLM deep-check passed — no further inconsistencies."}

    issues: list[ValidationIssue] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("ISSUE|"):
            continue
        parts = line.split("|", 3)
        if len(parts) == 4:
            _, sev, sec, msg = parts
            if sev in ("error", "warning", "info"):
                issues.append(ValidationIssue(
                    severity=sev,       # type: ignore[arg-type]
                    section_key=sec.strip(),
                    message=msg.strip(),
                ))

    summary = (
        f"LLM deep-check found {len(issues)} additional issue(s)."
        if issues else
        "LLM deep-check passed."
    )
    return {"issues": issues, "summary": summary}


# ── Node 7: finalise ──────────────────────────────────────────────────────────

def finalise(state: ValidatorState) -> dict:
    """Compute the final passed/failed verdict and compose a human-readable summary."""
    n_err  = sum(1 for i in state.issues if i.severity == "error")
    n_warn = sum(1 for i in state.issues if i.severity == "warning")
    n_info = sum(1 for i in state.issues if i.severity == "info")
    n_fill = sum(
        1 for i in state.issues
        if i.severity == "info" and "Auto-filled" in i.message
    )

    lines: list[str] = []
    if n_err:
        lines.append(f"{n_err} error(s) must be resolved before submission.")
    if n_warn:
        lines.append(f"{n_warn} warning(s) should be reviewed.")
    if n_fill:
        lines.append(f"{n_fill} section(s) had values auto-filled from cross-section consensus.")
    if n_info - n_fill:
        lines.append(f"{n_info - n_fill} informational note(s).")

    llm_summary = state.summary
    if llm_summary:
        lines.append(llm_summary)

    if not lines:
        lines.append("All cross-module checks passed — no issues found.")

    return {
        "passed": n_err == 0,
        "summary": " ".join(lines).strip(),
    }

