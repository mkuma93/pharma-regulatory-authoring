# pharma-regulatory-authoring

LLM-powered platform for ICH M4 CTD (Common Technical Document) regulatory authoring. Automates the generation, population, and cross-module validation of pharmaceutical submission documents using clinical trial data and ICH guidelines.

---

## Architecture

```
                        ┌─────────────────────┐
                        │   clinical data CSV  │
                        │  (uploaded per drug) │
                        └────────┬────────────┘
                                 │ POST /clinical-data/upload
                                 ▼
┌──────────────┐     ┌─────────────────────┐     ┌──────────────────┐
│  ICH4 Index  │────▶│  ICH4 Template      │────▶│  ICH4 Writer     │
│  (guidelines)│     │  (section templates)│     │  (doc generation)│
└──────────────┘     └─────────────────────┘     └──────────┬───────┘
                                                             │
                     ┌───────────────────────────────────────▼──────┐
                     │              ICH4 Content Worker              │
                     │         (Pub/Sub orchestration)               │
                     └───────────────────────────────────────────────┘
```

### Services

| Service | Path | Cloud Run | Description |
|---|---|---|---|
| **ICH4 Index** | `ICH4/index/` | `ich4-index` | Semantic search over ICH guidelines (LlamaIndex + GCS) |
| **ICH4 Template** | `ICH4/template/` | `ich4-template` | LLM-powered CTD section template generation with rich few-shot examples |
| **ICH4 Writer** | `ICH4/writer/` | `ich4-writer` | Fills templates with clinical data; includes Data Analyst Agent and cross-module validator |
| **ICH4 Content Worker** | `ICH4/content_worker/` | `ich4-content-worker` | Pub/Sub-driven orchestration across template → writer pipeline |
| **ICH4 Orchestrator** | `ICH4/orchestrator/` | `ich4-orchestrator` | End-to-end job orchestration |
| **CTD Structure** | `ctd_structure/` | `ctd-structure` | ICH M4 folder scaffold generator |

---

## Key Features

### Clinical Data Upload
Upload a CSV (any schema) for a specific drug/program:
```
POST /clinical-data/upload
  file               = trial.csv
  therapeutic_area   = neurology
  disease_type       = bells_palsy
  drug_name          = prednisolone
  bucket_name        = my-gcs-bucket
```
The LLM mapper automatically identifies column roles, CTD section keys, and `{{placeholder}}` names and stores a `manifest.json` alongside the CSV in GCS. Each drug gets its own isolated path:
```
gs://bucket/therapeutic-area/neurology/bells_palsy/prednisolone/
  clinical_data/
    trial.csv
    manifest.json
```

### Data Analyst Agent (Option C — Hybrid)
Sits between raw CSV data and the writer LLM. Four deterministic pandas tools, dispatched by LLM:

| Tool | Output example |
|---|---|
| `compute_proportion` | `71.2% (127/178)` or by-group table |
| `compute_mean_sd` | `2.1 ± 0.8 (n=178)` |
| `compute_crosstab` | Markdown table (treatment × outcome counts) |
| `compute_median_range` | `57.5 [40–77] (n=8)` |

No free Python code is generated — the LLM only dispatches to pre-coded tools.

### Cross-Module Validator
LangGraph-based consistency checker that runs after writing. Catches:
- Drug name mismatches across sections
- Demographic inconsistencies (warning)
- Efficacy stat discrepancies (error)
- Unfilled `{{placeholders}}` / `[DATA PENDING]` markers

### ICH4 Index
Semantic search over ICH M4 guidelines (E3, E9, M4, M4E, M4Q, M4S, S6, S7A, S9) using LlamaIndex + OpenAI embeddings, stored in GCS. Powers the context injection step during template generation.

---

## GCP Infrastructure

- **Project**: `pharma-reguatory-author`
- **Region**: `us-central1`
- **Services**: Cloud Run (all microservices)
- **Storage**: GCS buckets per service
- **Secrets**: Secret Manager (`OPENAI_API_KEY`, `LLAMA_CLOUD_API_KEY`)
- **Messaging**: Pub/Sub (`ctd-extraction-push` topic/subscription)

---

## Local Development

### Prerequisites
- Python 3.12+
- `gcloud` CLI authenticated
- OpenAI API key

### Run a service locally

```bash
# Writer service
cd ICH4/writer
pip install -r requirements.txt
OPENAI_API_KEY=sk-... GCP_PROJECT_ID=pharma-reguatory-author \
  python3 -m uvicorn api.app:app --host 127.0.0.1 --port 8080

# Template service
cd ICH4/template
pip install -r requirements.txt
OPENAI_API_KEY=sk-... GCP_PROJECT_ID=pharma-reguatory-author \
  python3 -m uvicorn api.app:app --host 127.0.0.1 --port 8081
```

### Run tests

```bash
# Writer (68 tests)
cd ICH4/writer && python3 -m pytest tests/ -v

# Template
cd ICH4/template && python3 -m pytest tests/ -v

# Index
cd ICH4/index && python3 -m pytest tests/ -v
```

---

## Deploy

Each service has its own deploy script:

```bash
# CTD Structure service
cd ctd_structure/deploy && bash tasks.sh deploy

# ICH4 Index
cd ICH4/index && bash deploy.sh

# ICH4 Writer
cd ICH4/writer && bash deploy.sh

# Rebuild ICH4 knowledge index
cd ICH4/index
set -a && source config/.env && set +a
python3 scripts/build_index.py
```

---

## Project Layout

```
life-science/
├── ICH4/
│   ├── index/           # Semantic search over ICH guidelines
│   ├── template/        # CTD section template generator
│   ├── writer/          # Document writer + validator + data analyst
│   │   ├── writer/
│   │   │   ├── clinical_uploader.py   # CSV upload + LLM mapper
│   │   │   ├── clinical_reader.py     # Manifest → analyst → writer context
│   │   │   ├── data_analyst.py        # 4 pandas tools + LLM dispatcher
│   │   │   ├── generator.py           # Section writer
│   │   │   └── models.py
│   │   ├── validator/                 # LangGraph cross-module validator
│   │   └── api/routes/
│   │       ├── write.py               # POST /write
│   │       ├── validate.py            # POST /validate
│   │       └── upload.py              # POST /clinical-data/upload
│   ├── orchestrator/    # End-to-end job orchestration
│   └── content_worker/  # Pub/Sub worker
├── ctd_structure/       # ICH M4 folder scaffold generator
├── clinical/            # CSV → CTD column mapper models
└── config/              # Shared settings
```

---

## Clinical Data Privacy

Patient-level CSV files are **not committed** to this repository (excluded via `.gitignore`). Upload them at runtime via `POST /clinical-data/upload` — they are stored securely in your GCS bucket under the program/drug path.
