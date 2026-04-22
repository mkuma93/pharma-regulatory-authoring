# Regulatory Audit Gaps — Life-Science CTD Authoring Platform

**Assessment Date:** April 19, 2026  
**Scope:** Controlled AI Regulatory Authoring — Traceability & Auditability Review  
**Standard:** FDA 21 CFR Part 11, ICH M4(R4), GCP Audit Readiness

---

## GAP 1 — No Immutable Audit Trail

**Severity:** Critical  
**Affected Files:** `ctd_structure/api/api_app.py`, `ICH4/content_worker/worker_app.py`

GCS status blobs (`session_state.json`, `extraction_status.json`, `{session_id}.json`) are mutable objects. They can be silently overwritten at any point. No append-only event log exists that spans all 6 services.

**Auditor Question:**  
_"Prove that section 2.5.3 was not modified after your regulatory scientist approved it."_  
**Current Answer:** Cannot.

**Required Fix:**  
- Implement an append-only event log (BigQuery table or Cloud Audit Log sink) that records every state transition with timestamp and actor.
- Enable GCS Object Versioning on all CTD `.md` documents and status blobs.

---

## GAP 2 — No User Identity or Accountability

**Severity:** Critical  
**Affected Files:** `ui/app.py`, `ctd_structure/api/api_app.py`, `clinical-analyst/app.py`

No `user_id`, `session_token`, or identity claim is included in any API request. GCS write operations carry only the service account identity (`811317821863-compute@developer.gserviceaccount.com`), making it impossible to attribute any action to an individual.

**Auditor Question:**  
_"Which employee generated and approved section 2.7.4, and when?"_  
**Current Answer:** Unknown.

**Required Fix:**  
- Add authenticated identity middleware (e.g., Google IAP or OIDC token forwarding) to all services.
- Propagate `user_id` and `timestamp` in every API request payload and log it with every GCS write.

---

## GAP 3 — No Human Approval Gate Before Content Generation

**Severity:** Critical  
**Affected Files:** `ui/coordinator.py`, `ICH4/content_worker/worker_app.py`

`ui/coordinator.py` immediately dispatches on `"approve"` intent without a confirmation step or reason capture. `ICH4/content_worker/worker_app.py` calls `/generate` on PubSub message receipt without verifying that templates were reviewed and approved by a qualified regulatory scientist.

```python
# ui/coordinator.py — no human checkpoint
if ir.intent == "approve":
    return CoordinatorDecision(outcome="proceed", intent="approve")
```

```python
# content_worker/worker_app.py — runs generation unconditionally
# No check: "were these templates reviewed?"
```

**Auditor Question:**  
_"Show GCP evidence that section 2.3 was reviewed by a qualified individual before inclusion in the NDA."_  
**Current Answer:** No such evidence exists.

**Required Fix:**  
- Implement a document lifecycle state machine: `DRAFT → IN_REVIEW → APPROVED → LOCKED`.
- Require an explicit human sign-off API call (with `user_id` + `reason`) before the content worker is permitted to run.
- Block the PubSub message from proceeding unless the referenced templates carry `status: APPROVED`.

---

## GAP 4 — Validator Output Not Persisted

**Severity:** High  
**Affected Files:** `ICH4/writer/validator/graph.py`, `ICH4/content_worker/worker_app.py`

The 9-stage LangGraph cross-module validator runs a comprehensive check (consistency, ICH M4 coverage, ground-truth figure matching, semantic coherence) but its output exists only in the HTTP response JSON. The validation report is **never written to GCS**. If validation fails, there is no immutable artifact for an auditor to inspect.

**Auditor Question:**  
_"What issues, if any, did your validation system flag for section 2.7.4 before it was submitted?"_  
**Current Answer:** No record.

**Required Fix:**  
- After every validator run, write the full `ValidationResult` (passed/failed, issues list, summary) to:  
  `gs://{bucket}/therapeutic-area/{ta}/{dis}/{drug}/validation/{session_id}/{section_key}.json`
- If `validation.passed == False`, block publication and require a human override with documented reason.

---

## GAP 5 — Prompt and Model Version Not Tracked

**Severity:** High  
**Affected Files:** `ICH4/writer/writer/generator.py`, `ICH4/template/template/generator.py`, `ui/coordinator.py`, all services using `ChatOpenAI`

Prompts are hardcoded strings in Python files with no hash or version identifier. The LLM model is specified as `gpt-4o-mini` without a date-pinned version string. OpenAI updates models server-side, meaning identical inputs can produce different outputs across deployments.

```python
# ICH4/writer/writer/generator.py
_SYSTEM_PROMPT = """\
You are a senior pharmaceutical regulatory scientist...
"""  # No version; changes lose history

# All services
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.0)  # No version pin
```

**Auditor Question:**  
_"Why does your June CTD submission differ from the April CTD for the same drug and same data?"_  
**Current Answer:** Cannot explain.

**Required Fix:**  
- Pin GPT model versions explicitly (e.g., `gpt-4o-mini-2024-07-18`) in all Dockerfiles and `requirements.txt`.
- At service startup, compute `SHA-256` of each prompt string and log it alongside every LLM call.
- Store prompt content in versioned config files (e.g., GCS or Git), not inline Python strings.

---

## GAP 6 — No Clinical Data Hash Verification

**Severity:** High  
**Affected Files:** `clinical/ingestion.py`, `clinical/storage.py`, `ICH4/writer/writer/clinical_reader.py`

CSVs are stored in GCS at upload time, but no cryptographic hash is computed or recorded. At resolution and writing time, `clinical_reader.py` loads the CSV without verifying it matches what was originally registered in the manifest.

**Auditor Question:**  
_"Prove that the clinical CSV used to compute '72.5% recovery rate' on April 15 is the same file you uploaded on January 15."_  
**Current Answer:** Cannot.

**Required Fix:**  
- Compute `SHA-256` of every uploaded CSV at ingestion time; store it in `manifest.json` as `source_hash`.
- At resolution time (`/resolve`), re-hash the loaded CSV and assert it equals `manifest.source_hash` before proceeding.
- Log any hash mismatch as a critical audit event and refuse computation.

---

## GAP 7 — No Segregation of Duties

**Severity:** High  
**Affected Files:** All services (single service account: `811317821863-compute@developer.gserviceaccount.com`)

The same service account is used across all 6 services, meaning the entity that generates content is the same entity that can approve and publish it. FDA 21 CFR Part 11 and ICH GCP require that the author, reviewer, and approver are distinct roles.

**Auditor Question:**  
_"Can the same person who wrote section 2.5 also approve it for submission?"_  
**Current Answer:** Yes — there is no technical control preventing this.

**Required Fix:**  
- Define three IAM roles: `ctd-author`, `ctd-reviewer`, `ctd-approver`.
- Enforce role-based API access: `/generate` requires `ctd-author`; `/approve` requires `ctd-approver`.
- Block self-approval: the `user_id` that submitted content cannot be the same `user_id` that approves it.

---

## GAP 8 — LLM Column Mapping Not Versioned or Human-Reviewed

**Severity:** Medium  
**Affected Files:** `clinical/mapper.py`

The LLM-generated column mapping (`manifest.json`) is produced by GPT-4o-mini from CSV headers and sample rows. The mapping prompt version is not recorded, the output is not reviewed by a data manager before use, and the mapping can silently change if the CSV or prompt changes.

**Required Fix:**  
- Log the mapper prompt hash alongside every manifest entry.
- Add a mandatory `mapping_reviewed_by` + `mapping_reviewed_at` field to each `ColumnMapping` before it is used in resolution or writing.

---

## GAP 9 — ICH Index Queries Not Versioned

**Severity:** Medium  
**Affected Files:** `ICH4/index/api/app.py`, `ICH4/content_pipeline/main.py`

The RAG index queries to the ICH guideline knowledge base are not timestamped, and the version of the guideline documents in the index is not recorded. LlamaIndex retrieval is non-deterministic; the same query on a re-indexed corpus may return different chunks.

**Required Fix:**  
- Record the index build timestamp (or a content hash of the index corpus) alongside every query.
- Store the retrieved guideline chunks used for each template generation as a GCS artifact.
- Take monthly snapshots of the index to allow retrospective reproducibility.

---

## GAP 10 — Extension Functions Not Audited or Registered

**Severity:** Medium  
**Affected Files:** `clinical-analyst/app.py`

LLM-generated extension computation functions pass AST + LLM review and are then hot-registered in the process. There is no persistent registry of which extensions were used during which computation session, and no version history if an extension is updated.

**Required Fix:**  
- Persist every accepted extension function to GCS: `clinical_data/extensions/{hash}.py`.
- Log `{extension_hash, session_id, user_id, timestamp}` when an extension is registered and when it is invoked.
- Require explicit approval (different `user_id` from proposer) before an extension can be used in production resolution.

---

## Summary

| # | Gap | Severity | FDA Risk |
|---|-----|----------|----------|
| 1 | No immutable audit trail | Critical | Cannot prove document integrity post-approval |
| 2 | No user identity / accountability | Critical | Cannot assign responsibility for actions |
| 3 | No human approval gate | Critical | No GCP evidence of qualified review |
| 4 | Validator output not persisted | High | No record of validation failures/passes |
| 5 | Prompt & model not versioned | High | Cannot explain CTD version differences |
| 6 | No clinical data hash verification | High | Cannot prove data integrity at computation time |
| 7 | No segregation of duties | High | Author can self-approve; violates 21 CFR Part 11 |
| 8 | Column mapping not reviewed | Medium | Silently wrong mappings undetectable |
| 9 | ICH index queries not versioned | Medium | Cannot reproduce guideline context used |
| 10 | Extension functions not audited | Medium | Unverified computation code in production |

---

## What Is Already Defensible

- Clinical statistics are computed by **deterministic pandas**, never by LLM
- ICH M4(R4) folder hierarchy is **hardcoded** — LLM cannot hallucinate section names
- Writer system prompt enforces **evidence-only** numerics with `[DATA PENDING]` for missing data
- Template placeholders are **source-typed** (`[SOURCE: clinical_data]` vs `[SOURCE: regulatory_context]`)
- 9-stage cross-module **validator** checks consistency, ICH coverage, and ground-truth figure matching
- `manifest_snapshot.json` preserves the manifest state before each update
- AST-level blocking of unsafe imports in generated extension code
