# Build context is the repo root.
# gcloud builds submit <repo_root> --config=ctd_structure/api/Dockerfile

FROM python:3.11-slim AS builder

WORKDIR /build
COPY ctd_structure/api/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Packages used by the API
COPY ctd_structure/__init__.py  ctd_structure/__init__.py
COPY ctd_structure/structure.py ctd_structure/structure.py
COPY ctd_structure/scaffold.py  ctd_structure/scaffold.py
COPY clinical/ clinical/
COPY template/ template/

# Coordinator Pydantic models (CoordinatorDecision used by action endpoints)
# The LangGraph runner (run_coordinator) lives in ui/ — the API only needs the model.
COPY ctd_structure/deploy/main.py main.py

# API application
COPY ctd_structure/api/api_app.py api_app.py

ENV PYTHONPATH=/app
ENV PORT=8081

RUN useradd --create-home appuser
USER appuser

CMD ["sh", "-c", "uvicorn api_app:app --host 0.0.0.0 --port ${PORT}"]
