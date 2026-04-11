# pharma-regulatory-authoring

LLM-powered platform for ICH M4(R4) CTD (Common Technical Document) regulatory authoring. Automates folder structure generation, clinical data integration, section content generation, and cross-module validation using ICH guidelines and real trial data.

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
| **ICH4 Orchestrator** | `ICH4/orchestrator/` | `ich4-orchestrator` | 8082 | End-to-end template generation jobs |
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
| `generate_template` | `POST ich4-orchestrator/generate` | Preview ICH-grounded templates for a program (no write) |
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

### LangGraph Coordinator (`ui/main.py`, shared by UI)
Two-node graph: `understand` → `decide`.
- **understand**: classifies intent + extracts slots (ta, disease, drug, section_keys, module_filter)
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

# ICH4 Orchestrator
gcloud builds submit . --config=ICH4/orchestrator/cloudbuild.yaml
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
│   ├── orchestrator/           # End-to-end job orchestrator
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
