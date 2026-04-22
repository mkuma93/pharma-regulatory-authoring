# pharma-regulatory-authoring

LLM-powered platform for ICH M4(R4) CTD (Common Technical Document) regulatory authoring. Automates folder structure generation, clinical data integration, section content generation, cross-module validation, and in-browser document reading — all from a single Gradio chat interface backed by Cloud Run microservices.

---

## Architecture

```
User (browser — IAP-protected)
     │
     ▼
┌────────────────────────────────────────┐
│           reguatory-ui                 │
│  Gradio chat shell + LangGraph         │
│  coordinator (intent classification)   │
└──────┬──────────┬───────────┬──────────┘
       │          │           │
       ▼          ▼           ▼
┌──────────┐ ┌────────────┐ ┌──────────────────┐
│ ctd-api  │ │ich4-writer │ │clinical-analyst  │
│ FastAPI  │ │FastAPI     │ │FastAPI           │
│ (struct) │ │(doc gen +  │ │(CSV stats + LLM) │
└──────────┘ │ validator +│ └──────────────────┘
             │ doc reader)│
             └─────┬──────┘
                   │ Pub/Sub
             ┌─────▼──────────────┐
             │ ich4-content-worker│
             │ (Pub/Sub consumer) │
             └─────┬──────────────┘
                   │
       ┌───────────┼───────────┐
       ▼           ▼           ▼
 ┌──────────┐ ┌──────────┐  (GCS)
 │ich4-index│ │ich4-temp-│
 │ (ICH RAG)│ │late      │
 └──────────┘ └──────────┘
```

All services are **Cloud Run** (stateless, `--no-allow-unauthenticated`). Session state and documents live in **GCS**. The UI is protected by **Identity-Aware Proxy (IAP)**.

---

## Services

| Service | Directory | Cloud Run name | Port | Role |
|---|---|---|---|---|
| **UI** | `ui/` | `reguatory-ui` | 8080 | Gradio chat + LangGraph coordinator + document viewer |
| **CTD API** | `ctd_structure/api/` | `ctd-api` | 8081 | CTD structure, scaffold, session state, Pub/Sub dispatch |
| **ICH4 Content Pipeline** | `ICH4/content_pipeline/` | `ich4-content-pipeline` | 8082 | Aggregates ICH index + template writer |
| **ICH4 Writer** | `ICH4/writer/` | `ich4-writer` | 8083 | Section writing, data analyst, validator, document reader |
| **Clinical Analyst** | `clinical-analyst/` | `clinical-analyst` | 8084 | Clinical CSV analysis (pandas + GPT-4o) |
| **ICH4 Content Worker** | `ICH4/content_worker/` | `ich4-content-worker` | — | Pub/Sub-driven async pipeline worker |
| **ICH4 Index** | `ICH4/index/` | `ich4-index` | 8080 | Semantic search over ICH M4 guidelines (RAG) |
| **ICH4 Template** | `ICH4/template/` | `ich4-template` | 8080 | ICH-grounded section template generator |

---

## Intent Routing

The `reguatory-ui` coordinator classifies every user message and routes it to the correct downstream service:

| Intent | Downstream call | Description |
|---|---|---|
| `extract` | `POST ctd-api/extract` | Publish extraction job to Pub/Sub; builds ICH M4 folder hierarchy |
| `approve` | `POST ctd-api/approve` | Commit folder structure as shared canonical template in GCS |
| `disapprove` | `POST ctd-api/disapprove` | Enter feedback loop for targeted ICH re-query |
| `copy` | `POST ctd-api/copy` | Scaffold canonical template into a program directory |
| `write` | `POST ctd-api/write` | Publish content-generation job to Pub/Sub |
| `status` | `GET ctd-api/status_query` | Human-readable extraction + content job status |
| `rewrite_section` | `POST ich4-writer/write` | Regenerate specific CTD section documents |
| `analyze_data` | `POST clinical-analyst/analyze` | Statistical + narrative analysis of clinical CSV data |
| `clarify` / `help` | _(handled in UI)_ | Coordinator asks for more info or shows help |

---

## GCS Layout

```
gs://pharma-reguatory-author-life-science/
├── ctd_structure/
│   ├── ctd/                              # Canonical ICH M4 folder template (.keep markers)
│   └── users/{session_id}/
│       ├── session_state.json            # Persisted UI state (incl. program_scaffold_exists)
│       ├── extraction_status.json
│       └── ctd/                          # Per-user draft structure
└── therapeutic-area/
    └── {ta}/{disease}/{drug}/
        ├── ctd/                          # Scaffolded program folder structure (.keep markers)
        ├── clinical_data/
        │   ├── {trial}.csv
        │   └── manifest.json             # Column mappings + CTD section keys + positive_value
        ├── templates/                    # ICH-grounded section templates (.md)
        │   ├── module2/                  # 2.1–2.7 overview and summary sections
        │   └── module5/                  # 5.1–5.4 clinical study reports
        ├── analysis/                     # Resolved clinical data placeholders
        └── content_status/
            ├── {session_id}.json         # Per-run status
            └── latest.json              # Always mirrors the most recent run status
```

---

## Key Components

### LangGraph Coordinator (`ui/coordinator.py`)
Two-node graph: `understand` → `decide`.
- **understand**: classifies intent + extracts slots (`therapeutic_area`, `disease_type`, `drug_name`, `section_keys`)
- **decide**: checks prerequisites; returns `outcome=clarify` if anything is missing, `outcome=proceed` otherwise

### CTD API (`ctd_structure/api/api_app.py`)
Pure action executor. Every endpoint returns:
```json
{ "reply": "<markdown for chat>", "state_patch": { "<key>": "<value>" } }
```
`state_patch` is merged into `gr.State` in the UI — this is how `folder_paths`, `content_program`, `program_scaffold_exists`, and `awaiting_feedback` propagate back to the browser session. The `program_scaffold_exists` flag is persisted to GCS so pre-flight checks survive page reloads.

### Clinical Analyst (`clinical-analyst/app.py`)
Reads clinical CSV from GCS via `manifest.json`, computes per-column pandas stats, then calls GPT-4o for a structured narrative:
- `POST /analyze` — demographics, efficacy, safety, benefit-risk synthesis
- `POST /query` — free-form Q&A over the data
- `POST /resolve` — compute ground-truth placeholder values (proportions, mean ± SD) from CSV

### Data Analyst Agent (`ICH4/writer/writer/data_analyst.py`)
Four deterministic pandas tools dispatched by LLM — no free code generation:

| Tool | Example output |
|---|---|
| `compute_proportion` | `71.2% (127/178)` |
| `compute_mean_sd` | `2.1 ± 0.8 (n=178)` |
| `compute_crosstab` | Markdown table |
| `compute_median_range` | `57.5 [40–77] (n=8)` |

### Cross-Module Validator (`ICH4/writer/validator/`)
LangGraph graph that runs after writing. Catches drug name mismatches, demographic inconsistencies, efficacy stat discrepancies, and unfilled `{{placeholder}}` markers across modules. `compiled.invoke()` always returns a `dict`; the graph handles this correctly.

### Document Reader (`ICH4/writer/api/routes/documents.py`)
Two read-only endpoints on `ich4-writer`:
- `GET /documents` — lists all generated `.md` template sections for a program
- `GET /documents/read` — returns the full markdown content of a specific section

### Clinical CSV Column Mapper (`clinical/mapper.py`)
LLM-powered mapper that parses uploaded CSVs and maps columns to CTD placeholder keys. Uses `json-repair` with up to 3 retries to handle malformed LLM JSON responses.

### Content Status (`ich4-content-worker`)
`_write_status()` automatically mirrors every per-session write to `latest.json` so the UI always shows the most recent run regardless of which session triggered it.

---

## UI Features

### Chat Interface
Natural language commands — e.g. *"Set up authoring for neurology / bells palsy / prednisolone"* or *"Generate CTD section content"*. The LangGraph coordinator handles intent classification, slot extraction, and prerequisite checking.

### 🔬 Clinical Data Upload
Upload a trial CSV file. The LLM mapper automatically identifies columns, maps them to CTD placeholder keys, and registers them in a manifest. Shows **Mapped columns** and **Total datasets** on success.

### 📄 Generated Documents
Browse and read generated CTD section content without GCS access:
1. Enter the program (Therapeutic Area / Disease / Drug Name) — auto-populated from active session
2. Click **Load Sections** — lists all generated sections grouped by module
3. Select a section from the dropdown — full markdown rendered inline

### Auto-poll
The UI polls for content generation status every 20 seconds and posts a completion message to chat automatically.

---

## GCP Infrastructure

| Resource | Value |
|---|---|
| Project | `pharma-reguatory-author` |
| Region | `us-central1` |
| Compute | Cloud Run (all services, min-instances=0, `--no-allow-unauthenticated`) |
| Storage | GCS `pharma-reguatory-author-life-science` |
| Messaging | Pub/Sub topics: `ctd-extraction`, `ich4-content-generation` |
| Secrets | Secret Manager: `OPENAI_API_KEY` |
| Registry | Artifact Registry `us-central1-docker.pkg.dev/pharma-reguatory-author/ich4` |
| Access | Identity-Aware Proxy (IAP) on `reguatory-ui` |
| LLM | OpenAI `gpt-4o-mini` |

---

## Prerequisites

- Google Cloud project with billing enabled
- `gcloud` CLI authenticated (`gcloud auth login`)
- Secret Manager secret `OPENAI_API_KEY` in the project
- Pub/Sub topics `ctd-extraction` and `ich4-content-generation` created
- IAP enabled on `reguatory-ui` with authorised user emails

---

## Deploy

Each service has its own `cloudbuild.yaml`. Submit from the **repo root**:

```bash
# UI (Gradio + coordinator)
gcloud builds submit . --config=ui/cloudbuild.yaml --project=pharma-reguatory-author

# CTD API (structure backend)
gcloud builds submit . --config=ctd_structure/api/cloudbuild.yaml --project=pharma-reguatory-author

# Clinical Analyst
gcloud builds submit . --config=clinical-analyst/cloudbuild.yaml --project=pharma-reguatory-author

# ICH4 Writer (doc gen + validator + document reader)
gcloud builds submit . --config=ICH4/writer/cloudbuild.yaml --project=pharma-reguatory-author

# ICH4 Content Worker
gcloud builds submit . --config=ICH4/content_worker/cloudbuild.yaml --project=pharma-reguatory-author

# ICH4 Index
gcloud builds submit . --config=ICH4/index/cloudbuild.yaml --project=pharma-reguatory-author

# ICH4 Template
gcloud builds submit . --config=ICH4/template/cloudbuild.yaml --project=pharma-reguatory-author

# ICH4 Content Pipeline
gcloud builds submit . --config=ICH4/content_pipeline/cloudbuild.yaml --project=pharma-reguatory-author
```

---

## Project Layout

```
life-science/
├── ui/                         # Gradio UI + LangGraph coordinator
│   ├── app.py                  # Chat shell, intent routing, API calls, document viewer
│   ├── coordinator.py          # LangGraph graph (understand → decide)
│   ├── Dockerfile
│   └── cloudbuild.yaml
├── ctd_structure/
│   ├── api/                    # FastAPI action backend (CTD operations + session state)
│   │   ├── api_app.py
│   │   ├── Dockerfile
│   │   └── cloudbuild.yaml
│   ├── scaffold.py             # GCS folder marker writer
│   └── structure.py            # ICH M4 structure extractor + refiner
├── clinical-analyst/           # Clinical data analysis service
│   ├── app.py
│   ├── Dockerfile
│   └── cloudbuild.yaml
├── ICH4/
│   ├── index/                  # ICH guidelines RAG (LlamaIndex + GCS)
│   ├── template/               # ICH-grounded template generator
│   ├── writer/                 # Document writer + data analyst + validator + doc reader
│   │   ├── api/routes/
│   │   │   ├── write.py        # POST /write
│   │   │   ├── documents.py    # GET /documents, GET /documents/read
│   │   │   ├── patch.py        # POST /patch
│   │   │   ├── upload.py       # POST /clinical-data/upload
│   │   │   └── validate.py     # POST /validate
│   │   ├── validator/          # LangGraph cross-module validator
│   │   └── writer/             # Section generator + data analyst tools
│   ├── content_pipeline/       # Aggregates ICH index + template writer
│   └── content_worker/         # Pub/Sub consumer — orchestrates full generation run
├── clinical/                   # Clinical CSV → CTD column mapper (with json-repair)
├── ctd/                        # Local ICH M4 CTD reference folder structure
├── data/                       # Local input/output for scripts
├── scripts/                    # Utility scripts (build_index, create_ctd, etc.)
├── tests/
│   ├── test_ctd_structure/
│   ├── test_knowledge/
│   ├── test_pipelines/
│   └── test_agents/
└── Findings/                   # Presentation assets
```

---

## Clinical Data Privacy

Patient-level CSV files are **not committed** to this repository (excluded via `.gitignore`). Upload them at runtime via the **🔬 Clinical Data Upload** panel in the UI. Files are stored in GCS under `therapeutic-area/{ta}/{disease}/{drug}/clinical_data/`.

---

## Demo Program

The reference demo uses:
- **Therapeutic Area**: `neurology`
- **Disease**: `bells_palsy`
- **Drug**: `prednisolone`
- **Trial data**: Bell's Palsy clinical trial CSV (494 patients)
- **Generated sections**: 17 (Module 2: 7 sections, Module 5: 4 sections + subsections)


---

## Architecture

```
User (browser)
     │
     ▼
┌────────────────────────────────────────┐
│           reguatory-ui                 │
│  Gradio chat shell + LangGraph         │
│  coordinator (intent classification)   │
└──────┬──────────┬───────────┬──────────┘
       │          │           │
       ▼          ▼           ▼
┌──────────┐ ┌──────────┐ ┌──────────────────┐
│ ctd-api  │ │ich4-orch.│ │clinical-analyst  │
│ FastAPI  │ │FastAPI   │ │FastAPI           │
│ (struct) │ │(template)│ │(CSV stats + LLM) │
└──────────┘ └────┬─────┘ └──────────────────┘
                  │
            ┌─────▼──────┐
            │ ich4-writer │
            │ FastAPI     │
            │ (doc gen +  │
            │  validator) │
            └─────┬───────┘
                  │ Pub/Sub
            ┌─────▼──────────────┐
            │ ich4-content-worker│
            │ (Pub/Sub consumer) │
            └────────────────────┘
                  │
            ┌─────▼──────┐
            │ ich4-index  │
            │ (ICH guide- │
            │  lines RAG) │
            └────────────┘
```

All services are **Cloud Run** (stateless). Session state and documents live in **GCS**.

---

## Services

| Service | Directory | Cloud Run name | Port | Role |
|---|---|---|---|---|
| **UI** | `ui/` | `reguatory-ui` | 8080 | Gradio chat + LangGraph coordinator |
| **CTD API** | `ctd_structure/api/` | `ctd-api` | 8081 | CTD structure, scaffold, Pub/Sub dispatch |
| **ICH4 Content Pipeline** | `ICH4/content_pipeline/` | `ich4-content-pipeline` | 8082 | Aggregates ICH index + template writer for content generation |
| **ICH4 Writer** | `ICH4/writer/` | `ich4-writer` | 8083 | Section writing, data analyst, validator |
| **Clinical Analyst** | `clinical-analyst/` | `clinical-analyst` | 8084 | Clinical CSV analysis (pandas + GPT-4o) |
| **ICH4 Content Worker** | `ICH4/content_worker/` | `ich4-content-worker` | — | Pub/Sub-driven pipeline worker |
| **ICH4 Index** | `ICH4/index/` | `ich4-index` | 8080 | Semantic search over ICH guidelines (RAG) |
| **ICH4 Template** | `ICH4/template/` | `ich4-template` | 8080 | ICH-grounded section template generator |

> `ctd_structure/deploy/app.py` — legacy monolith (being deprecated; replaced by `ui/` + `ctd_structure/api/`)

---

## Intent Routing

The `reguatory-ui` coordinator classifies every user message into one of these intents and routes it to the correct downstream service:

| Intent | Downstream call | Description |
|---|---|---|
| `extract` | `POST ctd-api/extract` | Publish extraction job to Pub/Sub; builds ICH M4 folder hierarchy |
| `approve` | `POST ctd-api/approve` | Commit folder structure as shared canonical template in GCS |
| `disapprove` | `POST ctd-api/disapprove` | Enter feedback loop for targeted ICH re-query |
| `copy` | `POST ctd-api/copy` | Scaffold canonical template into a program directory |
| `write` | `POST ctd-api/write` | Publish content-generation job to Pub/Sub |
| `status` | `GET ctd-api/status_query` | Human-readable extraction + content job status |
| `rewrite_section` | `POST ich4-writer/write` | Regenerate specific CTD section documents |
| `analyze_data` | `POST clinical-analyst/analyze` | Statistical + narrative analysis of clinical CSV data |
| `clarify` / `help` | _(handled in UI, no API call)_ | Coordinator asks for more info or shows help |

---

## GCS Layout

```
gs://pharma-reguatory-author-life-science/
├── ctd_structure/
│   ├── ctd/                              # Canonical ICH M4 folder template (.keep markers)
│   └── users/{session_id}/
│       ├── session_state.json
│       ├── extraction_status.json
│       └── ctd/                          # Per-user draft structure
└── therapeutic-area/
    └── {ta}/{disease}/{drug}/
        ├── ctd/                          # Scaffolded program folder structure
        ├── clinical_data/
        │   ├── {trial}.csv
        │   └── manifest.json             # Column mappings + CTD section keys
        ├── templates/                    # ICH-grounded section templates
        ├── documents/                    # Generated CTD section documents
        └── content_status/
            └── latest.json              # Content generation job status
```

---

## Key Components

### LangGraph Coordinator (`ui/coordinator.py`)
Two-node graph: `understand` → `decide`.
- **understand**: classifies intent + extracts slots (ta, disease, drug, section_keys)
- **decide**: checks prerequisites; returns `outcome=clarify` if anything is missing, `outcome=proceed` otherwise

### CTD API (`ctd_structure/api/api_app.py`)
Pure action executor. Every endpoint returns:
```json
{ "reply": "<markdown for chat>", "state_patch": { "<key>": "<value>" } }
```
`state_patch` is merged into `gr.State` in the UI — this is how e.g. `folder_paths`, `content_program`, and `awaiting_feedback` propagate back to the browser session.

### Clinical Analyst (`clinical-analyst/app.py`)
Reads clinical CSV from GCS via `manifest.json`, computes per-column pandas stats, then calls GPT-4o for a structured narrative:
- `POST /analyze` — demographics, efficacy, safety, benefit-risk synthesis
- `POST /query` — free-form Q&A over the data

### Data Analyst Agent (`ICH4/writer/writer/data_analyst.py`)
Four deterministic pandas tools dispatched by LLM (no free code generation):

| Tool | Example output |
|---|---|
| `compute_proportion` | `71.2% (127/178)` |
| `compute_mean_sd` | `2.1 ± 0.8 (n=178)` |
| `compute_crosstab` | Markdown table |
| `compute_median_range` | `57.5 [40–77] (n=8)` |

### Cross-Module Validator (`ICH4/writer/validator/`)
LangGraph graph that runs after writing. Catches drug name mismatches, demographic inconsistencies, efficacy stat discrepancies, and unfilled `{{placeholder}}` markers across modules.

---

## GCP Infrastructure

| Resource | Value |
|---|---|
| Project | `pharma-reguatory-author` |
| Region | `us-central1` |
| Compute | Cloud Run (all services, min-instances=0) |
| Storage | GCS bucket `pharma-reguatory-author-life-science` |
| Messaging | Pub/Sub topics: `ctd-extraction`, `ich4-content-generation` |
| Secrets | Secret Manager: `OPENAI_API_KEY` |
| Registry | Artifact Registry `us-central1-docker.pkg.dev/pharma-reguatory-author/cloud-run-source-deploy` |

---

## Deploy

Each service has its own `cloudbuild.yaml`. Submit from the **repo root**:

```bash
# UI (Gradio + coordinator)
gcloud builds submit . --config=ui/cloudbuild.yaml

# CTD API (structure backend)
gcloud builds submit . --config=ctd_structure/api/cloudbuild.yaml

# Clinical Analyst
gcloud builds submit . --config=clinical-analyst/cloudbuild.yaml

# ICH4 Index
gcloud builds submit . --config=ICH4/index/cloudbuild.yaml

# ICH4 Writer
gcloud builds submit . --config=ICH4/writer/cloudbuild.yaml  # (see ICH4/writer/deploy.sh)

# ICH4 Content Pipeline
gcloud builds submit . --config=ICH4/content_pipeline/cloudbuild.yaml
```

---

## Project Layout

```
life-science/
├── ui/                         # Gradio UI + LangGraph coordinator
│   ├── app.py                  # Gradio shell, intent routing, API calls
│   ├── main.py                 # LangGraph coordinator (understand → decide)
│   ├── Dockerfile
│   └── cloudbuild.yaml
├── ctd_structure/
│   ├── api/                    # FastAPI action backend (CTD operations)
│   │   ├── api_app.py
│   │   ├── Dockerfile
│   │   └── cloudbuild.yaml
│   ├── deploy/                 # Legacy monolith (being deprecated)
│   ├── scaffold.py             # GCS folder marker writer
│   └── structure.py            # ICH M4 structure extractor + refiner
├── clinical-analyst/           # Clinical data analysis service
│   ├── app.py
│   ├── Dockerfile
│   └── cloudbuild.yaml
├── ICH4/
│   ├── index/                  # ICH guidelines RAG (LlamaIndex + GCS)
│   ├── template/               # ICH-grounded template generator
│   ├── writer/                 # Document writer + data analyst + validator
│   ├── content_pipeline/       # Aggregates ICH index + template writer for content generation
│   └── content_worker/         # Pub/Sub consumer worker
├── clinical/                   # Clinical CSV → CTD column mapper
├── config/                     # Shared settings
├── Findings/                   # Presentation assets
└── tests/
    ├── test_ctd_structure/
    └── test_knowledge/
```

---

## Clinical Data Privacy

Patient-level CSV files are **not committed** to this repository (excluded via `.gitignore`). Upload them at runtime via the **🔬 Clinical Data Upload** panel in the UI, or directly via `POST ctd-api/upload_clinical`. Files are stored in GCS under `therapeutic-area/{ta}/{disease}/{drug}/clinical_data/`.
