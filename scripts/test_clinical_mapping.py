# ONE-TIME SCRIPT — manual end-to-end smoke test, not part of the test suite.
"""
scripts/test_clinical_mapping.py

End-to-end test of the clinical data pipeline:
  1. Read Bell's Palsy CSV
  2. Map columns → CTD sections via LLM  (clinical/mapper.py)
  3. Build in-memory ClinicalDataManifest (no GCS)
  4. Build canonical CTD structure from _ICH_M4_CANONICAL (no LLM)
  5. For EVERY CTD section, run _clinical_context_block() and print which
     sections received clinical data references
  6. Generate a single-section template (Module 2 / clinical overview) with
     the manifest injected so you can see the full content output

Run from workspace root:
    cd "/path/to/life-science"
    OPENAI_API_KEY=sk-... python3 scripts/test_clinical_mapping.py
"""
from __future__ import annotations

import os
import sys

# ── Add workspace root to path ────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Load API key from .env if not already set ─────────────────────────────────
if not os.environ.get("OPENAI_API_KEY"):
    env_file = os.path.join(ROOT, "ICH4", "index", "config", ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENAI_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    os.environ["OPENAI_API_KEY"] = val
                    break

import json
from datetime import date

from template.models import ProgramInfo
from clinical.models import ClinicalDataManifest
from clinical.mapper import map_csv_to_ctd
from template.generator import _clinical_context_block, _build_ich_requirements_block

# Import from ctd_structure (inside deploy package when run locally)
sys.path.insert(0, os.path.join(ROOT, "ctd_structure"))
from structure import _build_canonical_modules, CTDStructureOutput, EvaluationResult


# ── Config ────────────────────────────────────────────────────────────────────

PALSY_CSV  = os.path.join(ROOT, "clinical", "palsy", "Bells Palsy Clinical Trial.csv")
COVID_CSV  = os.path.join(ROOT, "clinical", "covid", "archive", "COVID clinical trials.csv")

PROGRAM = ProgramInfo(
    therapeutic_area="neurology",
    disease_type="bells_palsy",
    drug_name="prednisolone",
)

SEP = "=" * 72


def banner(title: str) -> None:
    print(f"\n{SEP}\n  {title}\n{SEP}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Map Bell's Palsy CSV
# ─────────────────────────────────────────────────────────────────────────────

banner("STEP 1 — Map Bell's Palsy CSV columns → CTD sections")
palsy_source = map_csv_to_ctd(
    filename="Bells Palsy Clinical Trial.csv",
    csv_content=open(PALSY_CSV, encoding="utf-8-sig").read(),
    program=PROGRAM,
    n_sample_rows=5,
)
palsy_source = palsy_source.model_copy(
    update={"gcs_path": "therapeutic-area/neurology/bells_palsy/prednisolone/clinical_data/Bells Palsy Clinical Trial.csv"}
)

print(f"\nstudy_type     : {palsy_source.study_type}")
print(f"ctd_section_keys: {palsy_source.ctd_section_keys}")
print(f"\nColumn mappings ({len(palsy_source.column_mappings)}):")
for cm in palsy_source.column_mappings:
    print(f"  [{cm.role:25s}] {cm.column_name!r:45s} → sections={cm.ctd_section_keys}  placeholder={{{{{cm.placeholder_key}}}}}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Map COVID registry CSV
# ─────────────────────────────────────────────────────────────────────────────

banner("STEP 2 — Map COVID registry CSV columns → CTD sections")
covid_source = map_csv_to_ctd(
    filename="COVID clinical trials.csv",
    csv_content=open(COVID_CSV, encoding="utf-8-sig").read(),
    program=ProgramInfo(
        therapeutic_area="infectious_disease",
        disease_type="covid_19",
        drug_name="antiviral_agent",
    ),
    n_sample_rows=5,
)
covid_source = covid_source.model_copy(
    update={"gcs_path": "therapeutic-area/infectious_disease/covid_19/antiviral_agent/clinical_data/COVID clinical trials.csv"}
)

print(f"\nstudy_type     : {covid_source.study_type}")
print(f"ctd_section_keys: {covid_source.ctd_section_keys}")
print(f"\nColumn mappings ({len(covid_source.column_mappings)}):")
for cm in covid_source.column_mappings:
    print(f"  [{cm.role:25s}] {cm.column_name!r:45s} → sections={cm.ctd_section_keys}  placeholder={{{{{cm.placeholder_key}}}}}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Build in-memory manifest + canonical CTD structure
# ─────────────────────────────────────────────────────────────────────────────

banner("STEP 3 — Build manifest & canonical CTD structure")

palsy_manifest = ClinicalDataManifest(
    program=PROGRAM,
    sources=[palsy_source],
    last_updated=date.today().isoformat(),
)

canonical_modules = _build_canonical_modules()
ctd_output = CTDStructureOutput(
    modules=canonical_modules,
    evaluation=EvaluationResult(passed=True, issues=[], summary="test run"),
)

all_sections: list[tuple[str, str, str]] = []  # (module_key, section_key, section_label)
for mod in ctd_output.modules:
    for sec in mod.sections:
        all_sections.append((mod.key, sec.key, sec.label))

print(f"\nCanonical CTD has {len(ctd_output.modules)} modules, {len(all_sections)} top-level sections")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3b — Load LlamaIndex query engine (if index exists)
# ─────────────────────────────────────────────────────────────────────────────

banner("STEP 3b — Load LlamaIndex query engine")

query_engine = None
try:
    from src.knowledge.indexer import load_index
    from src.knowledge.query_engine import build_query_engine
    ich_index = load_index()
    query_engine = build_query_engine(ich_index)
    print("LlamaIndex engine loaded from persisted index.")
except Exception as exc:
    print(f"Index not available (expected in CI / first run): {exc}")
    print("Templates will fall back to LLM training knowledge for ICH requirements.")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — For every CTD section, check which ones receive clinical context
# ─────────────────────────────────────────────────────────────────────────────

banner("STEP 4 — Clinical data coverage across CTD sections (Bell's Palsy manifest)")

matched: list[tuple[str, str]] = []
empty:   list[tuple[str, str]] = []

for mod_key, sec_key, sec_label in all_sections:
    block = _clinical_context_block(sec_key, palsy_manifest)
    if block:
        matched.append((sec_key, sec_label))
    else:
        empty.append((sec_key, sec_label))

print(f"\n✅ Sections WITH clinical data reference ({len(matched)}):")
for sk, sl in matched:
    print(f"   {sk:55s}  {sl}")

print(f"\n⬜ Sections without clinical data ({len(empty)}):")
for sk, sl in empty:
    print(f"   {sk:55s}  {sl}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Show full context block for 2–3 key sections
# ─────────────────────────────────────────────────────────────────────────────

banner("STEP 5 — Full clinical context blocks for key sections")

KEY_SECTIONS = [
    "5.3.5.1_controlled_clinical_study_reports",
    "2.7.3_summary_of_clinical_efficacy",
    "2.5_clinical_overview",
    "2.5.4_overview_of_efficacy",
    "2.5.5_overview_of_safety",
]

for sk in KEY_SECTIONS:
    block = _clinical_context_block(sk, palsy_manifest)
    if block:
        print(f"\n--- Context block for: {sk} ---")
        print(block)
    else:
        print(f"\n[no block matched] {sk}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Generate one real template for 2.7.3 to show LLM + clinical data
# ─────────────────────────────────────────────────────────────────────────────

banner("STEP 6 — Generate template for 2.7.3 with ICH index + clinical data injected")

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI


# Find the 2.7 (Clinical Summary) section in the canonical structure
target_module = next((m for m in ctd_output.modules if m.key == "module2"), None)
target_section = None
if target_module:
    target_section = next((s for s in target_module.sections if s.key.startswith("2.7")), None)

if target_section is None:
    print("Section 2.7.3 not found — printing module2 section keys:")
    if target_module:
        for s in target_module.sections:
            print(f"  {s.key}")
else:
    from template.generator import _get_module_persona, _SECTION_PROMPT_TMPL, _FEW_SHOT_EXAMPLES, _build_ich_requirements_block
    import json as _json

    sections_input = [
        {
            "section_key":   target_section.key,
            "section_label": target_section.label,
            "subsections":   [{"key": sub.key, "label": sub.label} for sub in target_section.subsections],
        }
    ]

    ich_block = _build_ich_requirements_block(query_engine, "module2", "Module 2 — Common Technical Document Summaries")
    if ich_block:
        print(f"\n[ICH index retrieved requirements for {target_section.key}]")
        print(ich_block[:800])
    else:
        print("\n[No index available — LLM training knowledge will be used for ICH requirements]")

    prompt = _SECTION_PROMPT_TMPL.format(
        few_shot_examples=_FEW_SHOT_EXAMPLES,
        drug_name=PROGRAM.drug_name,
        disease_type=PROGRAM.disease_type,
        therapeutic_area=PROGRAM.therapeutic_area,
        module_key="module2",
        module_label="Module 2 — Common Technical Document Summaries",
        sections_json=_json.dumps(sections_input, indent=2),
        ich_retrieved_requirements=ich_block,
    )

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.2,
                     api_key=os.environ["OPENAI_API_KEY"])
    response = llm.invoke([SystemMessage(content=_get_module_persona("module2")), HumanMessage(content=prompt)])
    raw = response.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    items = json.loads(raw)
    content = items[0]["content"]

    # Inject clinical data block
    extra = _clinical_context_block(target_section.key, palsy_manifest)
    if extra:
        content = content + "\n" + extra

    print(f"\nGenerated template for: {target_section.key}\n")
    print(content[:4000])  # Print first 4000 chars to keep output manageable
    if len(content) > 4000:
        print(f"\n... [{len(content) - 4000} more characters]")

print(f"\n\n{SEP}\nDone.\n{SEP}")
