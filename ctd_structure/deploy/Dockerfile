# Build context is the repo root.
# gcloud builds submit <repo_root> --config=ctd_structure/deploy/cloudbuild.yaml
#
# Directory layout seen by Docker at build time:
#   .                      ← repo root
#   ├── ctd_structure/
#   │   ├── __init__.py
#   │   ├── structure.py
#   │   ├── scaffold.py
#   │   └── deploy/
#   │       ├── app.py
#   │       ├── main.py
#   │       ├── requirements.txt
#   │       └── Dockerfile    ← this file
#   ├── clinical/
#   └── template/

# ── Stage 1: install dependencies ────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build
COPY ctd_structure/deploy/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy ctd_structure package
COPY ctd_structure/__init__.py  ctd_structure/__init__.py
COPY ctd_structure/structure.py ctd_structure/structure.py
COPY ctd_structure/scaffold.py  ctd_structure/scaffold.py

# Copy clinical data ingestion package
COPY clinical/ clinical/

# Copy template models (required by clinical/)
COPY template/ template/

# Copy Gradio app
COPY ctd_structure/deploy/app.py  app.py
COPY ctd_structure/deploy/main.py main.py

ENV PYTHONPATH=/app

# Cloud Run injects PORT at runtime; Gradio default is 7860 but we use 8080
ENV PORT=8080

# Run as non-root
RUN useradd --create-home appuser
USER appuser

CMD ["sh", "-c", "python app.py"]
