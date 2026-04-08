# Build context must be the ctd_structure/ directory (parent of deploy/).
# deploy.sh submits that context:
#   gcloud builds submit ctd_structure/ --config=ctd_structure/deploy/cloudbuild.yaml
#
# Directory layout seen by Docker at build time:
#   .                    ← ctd_structure/
#   ├── __init__.py
#   ├── structure.py
#   ├── scaffold.py
#   └── deploy/
#       ├── app.py
#       ├── requirements.txt
#       └── Dockerfile    ← this file

# ── Stage 1: install dependencies ────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build
COPY deploy/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy ctd_structure package
COPY __init__.py  ctd_structure/__init__.py
COPY structure.py ctd_structure/structure.py
COPY scaffold.py  ctd_structure/scaffold.py

# Copy Gradio app
COPY deploy/app.py  app.py
COPY deploy/main.py main.py

ENV PYTHONPATH=/app

# Cloud Run injects PORT at runtime; Gradio default is 7860 but we use 8080
ENV PORT=8080

# Run as non-root
RUN useradd --create-home appuser
USER appuser

CMD ["sh", "-c", "python app.py"]
