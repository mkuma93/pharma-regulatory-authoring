# Content Generation Bugs — Life-Science CTD Authoring Platform

**Date:** April 19, 2026  
**Scope:** End-to-end content generation pipeline (demo readiness review)  
**Focus:** Functional bugs causing silent failures or crashes, not regulatory compliance

---

## BUG 1 — Silent Template Generation Failure (Critical)

**File:** `ICH4/template/template/generator.py` (~line 660)  
**Likely cause of:** "Generation completed but 0 sections written"

The LLM is asked to return a JSON array where each `content` field is a full multi-page Markdown document. If the LLM outputs malformed JSON (unescaped newlines, special characters inside string values), `json.loads` throws an exception — which is **silently swallowed**:

```python
except Exception as exc:
    print(f"[template.generator] Warning: failed to generate templates for {module.key}: {exc}")
    # ↑ No raise — returns empty list silently
```

**Effect chain:**
1. `generate_program_templates()` returns `[]`
2. Orchestrator returns `templates: []`
3. Worker sees empty templates, logs a warning, skips writing
4. Worker writes `status: "done"` with `sections_written: 0`
5. UI shows **generation complete** — but nothing was produced

**Fix:** Replace the bare `print` with a raised `HTTPException(500)` or log at `ERROR` level and surface the failure to the worker so it marks status as `"failed"` rather than `"done"`.

---

## BUG 2 — Bucket Not Forwarded to Template Service (Critical)

**Files:** `ICH4/content_pipeline/main.py`, `ICH4/template/api/routes/generate.py`

The worker provides a `bucket` field → the orchestrator receives `body.bucket` → but the orchestrator calls the template service **without forwarding the bucket**:

```python
# content_pipeline/main.py — Step 2 payload
payload: dict = {
    "program": body.program.model_dump(),
    "ich_context": ich_context if ich_context else None,
    "include_clinical_data": body.include_clinical_data,
    "section_key_prefixes": body.section_key_prefixes,
    "prior_evidence": prior_evidence if prior_evidence else None,
    # ← "bucket" is missing here
}
```

The template service falls back to `settings.gcs_bucket_name` (env var). If that env var is not set in the Cloud Run deployment, `clinical_manifest = None` and all templates are generated **without clinical data context** — placeholders are created with `[SOURCE: clinical_data]` tags but with no column hints, so the writer has no statistics to fill them with.

**Fix:** Add `bucket: str = ""` to `GenerateRequest` in `template/api/routes/generate.py` and forward `"bucket": body.bucket` in the orchestrator payload. Pass it through to `load_manifest()`.

---

## BUG 3 — `{placeholder}` vs `{{placeholder}}` Inconsistency in System Rules

**File:** `ICH4/template/template/generator.py` (~line 100)

`_SYSTEM_RULES` explicitly instructs the LLM:

```python
_SYSTEM_RULES = """\
...
- Use {placeholder} syntax for information the document author must supply.
...
"""
```

This says **single braces**. But every few-shot example and `_SECTION_PROMPT_TMPL` uses **double braces** `{{placeholder}}`. The writer's placeholder extractor only matches double braces:

```python
# writer/writer/placeholder.py
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
```

If the LLM follows the rule text instead of the examples, it generates `{recovery_rate}` — and the writer finds **zero placeholders**. All content is passed through unfilled. No error is raised.

**Fix:** Change `_SYSTEM_RULES` to read `Use {{placeholder}} syntax`.

---

## BUG 4 — Drug Name With Braces Crashes Prompt Formatting

**File:** `ICH4/template/template/generator.py` (~line 640)

The prompt is built using Python string `.format()`:

```python
prompt = _SECTION_PROMPT_TMPL.format(
    drug_name=program.drug_name,
    disease_type=program.disease_type,
    ...
)
```

If `program.drug_name` (or any other field) contains `{` or `}` characters — for example `"ABC-{123}"` or any compound with a formula like `"NaHCO{3}"` — Python's `.format()` raises `KeyError` or `ValueError`. The exception propagates as an unhandled 500.

**Fix:** Escape brace characters before injection:

```python
safe_drug = program.drug_name.replace("{", "{{").replace("}", "}}")
```

Or switch the prompt template to use `string.Template` with `$variable` syntax to avoid the collision entirely.

---

## BUG 5 — Validator Runs Synchronously With No Timeout

**File:** `ICH4/writer/api/routes/write.py` (~line 90)

The 9-stage LangGraph validator runs **synchronously** inside the `/write` endpoint after all sections have been written:

```python
# Synchronous — no timeout, no async, no background task
validation = run_validator(
    documents=documents,
    llm=llm,
    resolved_values=request.resolved_values or {},
)
```

For a large multi-section write (10–20 sections), writing alone takes several minutes. The validator then fires 2 additional LLM calls (`check_ich_coverage` + `llm_deep_check`) synchronously on top. The content worker's service timeout is:

```python
_TIMEOUT = float(os.environ.get("SERVICE_TIMEOUT", "600"))  # 10 minutes
```

The combined write + validate time regularly exceeds 10 minutes for full Module 2+5 generation, causing the worker's HTTP call to the writer to time out. The worker then marks the job as `"failed"` with `transient: ReadTimeout` and Pub/Sub retries — potentially re-running the entire pipeline.

**Fix:** Either:
- Skip validator when `len(documents) > threshold` (e.g. 5 sections)
- Run validator as a background task and respond immediately with written documents
- Increase `SERVICE_TIMEOUT` to 1800 seconds (30 min) as a short-term fix

---

## BUG 6 — Evidence Namespace Label Truncated in Logs and Prompts

**File:** `ICH4/content_pipeline/main.py` (`_build_prior_evidence_block`)

```python
pass_id = ns.rsplit("_", 1)[-1]
# For ns = "program_neurology_bells_palsy_prednisolone_module2_clinical_summary"
# → pass_id = "summary"   (not "module2_clinical_summary")
label = pass_id.replace("_", " ").title()
# → "Summary"  instead of "Module2 Clinical Summary"
```

`rsplit("_", 1)` splits only at the **last** underscore. For multi-word pass IDs like `module2_clinical_summary`, only `"summary"` is captured. This affects the label injected into the LLM prompt for prior-evidence context — the LLM sees `[Evidence source: Summary]` which is ambiguous when multiple passes exist.

**Fix:** Extract the pass ID suffix by stripping the base namespace prefix:

```python
base_ns = _program_namespace(ta, dis, drug)  # known at call site
pass_id = ns[len(base_ns):].lstrip("_")      # → "module2_clinical_summary"
label   = pass_id.replace("_", " ").title()  # → "Module2 Clinical Summary"
```

---

## Summary

| # | Severity | File | Bug | Effect |
|---|----------|------|-----|--------|
| 1 | Critical | `ICH4/template/template/generator.py` | JSON parse failure silently swallowed | Zero templates returned; status shows "done" with 0 sections written |
| 2 | Critical | `ICH4/content_pipeline/main.py` | Bucket not forwarded to template service | No clinical data in templates; writer has no statistics to fill |
| 3 | High | `ICH4/template/template/generator.py` | `{placeholder}` in system rules vs `{{placeholder}}` in examples | Writer finds no placeholders; content passes through unfilled |
| 4 | High | `ICH4/template/template/generator.py` | `.format()` called with unescaped drug name | 500 crash if drug name contains `{` or `}` |
| 5 | High | `ICH4/writer/api/routes/write.py` | Validator runs synchronously with no timeout | Worker HTTP timeout on large writes; Pub/Sub retries entire pipeline |
| 6 | Low | `ICH4/content_pipeline/main.py` | `rsplit` truncates multi-word pass ID label | Ambiguous evidence source label in LLM prompts and logs |

**Bugs 1 and 2 are the most likely cause of a demo run producing "generation complete" with no output.**
